#! /usr/bin/env python3
"""unwrap_backend — pluggable phase-unwrapping backend for GMTSAR.

GMTSAR has always hard-coded the `snaphu` C binary (Chen & Zebker 2000) at the
one point in the pipeline where wrapped phase becomes unwrapped phase. This
module turns that one point into a dispatcher so a different solver can be
dropped in without touching any of the surrounding GMT grid plumbing.

Backends
--------
``snaphu``     — the bundled third-party C binary. Default; unchanged behaviour.
``whirlwind``  — https://github.com/scottstanie/whirlwind-insar, an MCF-based
                 unwrapper reported ~10x faster than SNAPHU. Ships prebuilt
                 binaries; `whirlwind --version` must be on PATH (or point
                 ``GMTSAR_WHIRLWIND_BIN`` at it).

Why this is a clean seam
------------------------
Both `snaphu.csh` and `utils/snaphu.py` bracket the unwrap call with pure GMT
work. Everything *before* produces exactly three things:

    phase.in    flat float32, native endian, row-major (snaphu FLOAT_DATA)
    corr.in     flat float32, native endian, row-major, 0 where masked out
    width       column count, from `gmt grdinfo -C phase_patch.grd` field 10

and everything *after* consumes exactly two:

    unwrap.out    flat float32 unwrapped phase   (-> gmt xyz2grd -ZTLf)
    conncomp.out  flat uint8 component labels    (-> gmt xyz2grd -ZTLu)

whirlwind reads and writes precisely those layouts natively (`--phase` +
`--cor` flat-binary inputs with `--cols`, `--out-format float`, and a non-TIFF
`--conncomp` path which it writes as one byte per pixel), so no format shims
are needed on either side.

Selection
---------
Resolved by :func:`select_backend`, in precedence order:

    1. the ``unwrapper`` argument threaded down from ``config.py``
    2. the ``GMTSAR_UNWRAPPER`` environment variable
    3. ``snaphu``

Per project rule 1 an unknown name is a hard error, never a fallback to snaphu.

whirlwind tuning knobs (env only, so config.py stays backend-agnostic):

    GMTSAR_WHIRLWIND_BIN     executable name/path            [whirlwind]
    GMTSAR_WHIRLWIND_NLOOKS  --nlooks override               [NCORRLOOKS from
                                                              snaphu.conf.brief]
    GMTSAR_WHIRLWIND_ARGS    extra args, shlex-split, appended last so they
                             win (e.g. "--goldstein-alpha 0.7 --interpolate")

Known behavioural differences vs snaphu — read before trusting a product
-----------------------------------------------------------------------
1. ``defomax`` has no whirlwind equivalent. GMTSAR maps defomax>0 onto snaphu's
   DEFO statistical-cost mode with DEFOMAX_CYCLE; whirlwind's MCF solver has no
   such ceiling. The value is announced and ignored (loudly — see
   :func:`_whirlwind_argv`), not silently dropped.
2. The whole SAR-geometry block of snaphu.conf.brief (ORBITRADIUS, BASELINE,
   NEARRANGE, DR/DA, LAMBDA, ...) is unused. Only NCORRLOOKS carries over, as
   ``--nlooks``.
3. Output is not bit-comparable with snaphu and never will be — these are
   different algorithms. The absolute 2*pi*k offset between connected components
   is undefined in whirlwind, as it is in snaphu.
4. ``--mask`` is available but deliberately unused, and passing it naively
   would be a bug. whirlwind takes a flat uint8 mask (nonzero = valid) as well
   as a TIFF, chosen by extension (whirlwind-cli/src/lib.rs:570). But with no
   ``--mask`` it derives ``corr > 0`` itself, and an explicit ``--mask``
   *replaces* that default rather than intersecting with it. GMTSAR has
   already zeroed corr.in below `threshold_snaphu`, so the implicit mask
   reproduces the threshold mask for free — while a land-only mask would
   silently UN-mask every below-threshold pixel.
   The real gap is the landmask: snaphu.csh multiplies landmask_ra.grd into
   *phase* only, never into corr, so water pixels reach the solver with a
   zeroed phase and their original coherence. mask_def.grd is applied to corr
   and so is already covered. Fixing this needs one byte mask that is the AND
   of threshold-valid and land-valid — not a second mask file. Not done here.
5. whirlwind writes NaN only at masked pixels, i.e. where corr.in is 0 (the
   implicit `corr > 0` mask); snaphu writes a phase value there. That is the
   region utils/snaphu.py already NaNs by multiplying with mask2_patch.grd, so
   unwrap.grd coverage matches a snaphu run. A valid but unreliable pixel gets
   conncomp == 0 and still keeps its unwrapped phase, as in snaphu. The
   conncomp rules (`--conncomp-reliability`, default 0.5) change only the
   labels, never the phase; `--conncomp-reliability 0` labels every unwrapped
   pixel.

whirlwind >= 0.10.0 is required (checked by :func:`whirlwind_version`). Older
releases use a coarser cost table and different connected-component defaults.
"""

import os
import re
import shlex
import subprocess
import sys

from gmtsar_lib import run

BACKENDS = ('snaphu', 'whirlwind')

_DEFAULT_BACKEND = 'snaphu'


def select_backend(unwrapper=None):
    """Resolve which unwrapper to run: arg > GMTSAR_UNWRAPPER > 'snaphu'.

    Args:
        unwrapper: value threaded down from config.py, or None/'' /-999
            (the GMTSAR "unset" sentinel) to defer to the environment.

    Returns:
        one of :data:`BACKENDS`.

    Raises:
        SystemExit: on an unrecognised name, from either source. An unknown
            backend must not silently degrade to snaphu (project rule 1) —
            a typo'd `unwrapper = whirlwid` would otherwise produce a
            plausible-looking product from the wrong solver.
    """
    if unwrapper in (None, '', -999, '-999'):
        name = os.environ.get('GMTSAR_UNWRAPPER', _DEFAULT_BACKEND)
        source = 'GMTSAR_UNWRAPPER'
    else:
        name = str(unwrapper)
        source = 'config.py unwrapper'

    name = name.strip().lower()
    if name not in BACKENDS:
        sys.exit(f'UNWRAP: ERROR: unknown unwrapper {name!r} (from {source}); '
                 f'expected one of {", ".join(BACKENDS)}')
    return name


def run_unwrapper(backend, *, phase_in, corr_in, width, unwrap_out,
                  conncomp_out, defomax, sharedir):
    """Unwrap ``phase_in`` -> ``unwrap_out`` + ``conncomp_out``.

    The contract is the snaphu one described in the module docstring: flat
    float32 in, flat float32 + flat uint8 out, no GMT grids involved. Callers
    (utils/snaphu.py) do all grid conversion on either side.

    Args:
        backend      : from :func:`select_backend`.
        phase_in     : flat float32 wrapped phase, radians (snaphu FLOAT_DATA).
        corr_in      : flat float32 coherence in [0, 1], 0 where masked.
        width        : column count as a str or int.
        unwrap_out   : flat float32 output path.
        conncomp_out : flat uint8 output path.
        defomax      : GMTSAR maximum-discontinuity in cycles. 0 selects
            snaphu's smooth (-s) mode; >0 selects defo (-d) mode with
            DEFOMAX_CYCLE patched in. Has no effect under whirlwind (see
            module docstring, difference 1).
        sharedir     : GMTSAR sharedir, holding snaphu/config/snaphu.conf.brief.
    """
    if backend == 'snaphu':
        _run_snaphu(phase_in=phase_in, corr_in=corr_in, width=width,
                    unwrap_out=unwrap_out, conncomp_out=conncomp_out,
                    defomax=defomax, sharedir=sharedir)
    elif backend == 'whirlwind':
        _run_whirlwind(phase_in=phase_in, corr_in=corr_in, width=width,
                       unwrap_out=unwrap_out, conncomp_out=conncomp_out,
                       defomax=defomax, sharedir=sharedir)
    else:
        sys.exit(f'UNWRAP: ERROR: unhandled backend {backend!r}')


# ---------------------------------------------------------------------------
# snaphu (unchanged behaviour — lifted verbatim from utils/snaphu.py)
# ---------------------------------------------------------------------------

def _run_snaphu(*, phase_in, corr_in, width, unwrap_out, conncomp_out,
                defomax, sharedir):
    """The historical snaphu invocation. Byte-identical to snaphu.csh.

    defomax == 0 -> `-s` (smooth) against the stock snaphu.conf.brief;
    defomax  > 0 -> `-d` (defo) against a local copy with DEFOMAX_CYCLE
    rewritten. `file_shuttle`/`replace_strings` are imported lazily so this
    module stays importable without the full gmtsar_lib surface.
    """
    from gmtsar_lib import file_shuttle, replace_strings

    if float(defomax) == 0:
        run(f'snaphu {phase_in} {width} '
            f'-f {sharedir}/snaphu/config/snaphu.conf.brief '
            f'-c {corr_in} -o {unwrap_out} -v -s -g {conncomp_out}')
    else:
        file_shuttle(f'{sharedir}/snaphu/config/snaphu.conf.brief',
                     'snaphu.conf.brief', 'cp')
        replace_strings('snaphu.conf.brief',
                        'DEFOMAX_CYCLE', f'DEFOMAX_CYCLE {defomax}')
        run(f'snaphu {phase_in} {width} -f snaphu.conf.brief '
            f'-c {corr_in} -o {unwrap_out} -v -d -g {conncomp_out}')


# ---------------------------------------------------------------------------
# whirlwind
# ---------------------------------------------------------------------------

MIN_WHIRLWIND_VERSION = (0, 10, 0)

_INSTALL_HINT = (
    '  Install a release binary from '
    'https://github.com/scottstanie/whirlwind-insar/releases\n'
    '  or see https://github.com/scottstanie/whirlwind-insar#cli-installation\n'
    '  then put it on PATH or set GMTSAR_WHIRLWIND_BIN=/path/to/whirlwind')


def _whirlwind_bin():
    return os.environ.get('GMTSAR_WHIRLWIND_BIN', 'whirlwind')


def whirlwind_version():
    """Return whirlwind's version string, or exit if it isn't usable.

    Called up front rather than letting the unwrap itself fail: a missing
    binary halfway through a batch wastes the whole preceding pipeline, and
    the recorded version belongs in the log next to the products it made.
    A release older than :data:`MIN_WHIRLWIND_VERSION`, or a version string
    that can't be parsed, is a hard error too (project rule 1): older
    releases produce different phase and component labels.
    """
    exe = _whirlwind_bin()
    try:
        out = subprocess.run([exe, '--version'], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        sys.exit(
            f'UNWRAP: ERROR: unwrapper=whirlwind but {exe!r} is not runnable '
            f'({exc}).\n' + _INSTALL_HINT)
    version = out.stdout.decode().strip()
    m = re.fullmatch(r'whirlwind (\d+)\.(\d+)\.(\d+)\S*', version)
    minimum = '.'.join(map(str, MIN_WHIRLWIND_VERSION))
    if m is None or tuple(map(int, m.groups())) < MIN_WHIRLWIND_VERSION:
        sys.exit(
            f'UNWRAP: ERROR: {exe!r} reports {version!r}; GMTSAR needs '
            f'whirlwind >= {minimum}.\n' + _INSTALL_HINT)
    return version


def _ncorrlooks(sharedir):
    """Read NCORRLOOKS from the bundled snaphu.conf.brief.

    whirlwind's `--nlooks` plays the same role in its cost model as snaphu's
    NCORRLOOKS ("equivalent number of independent looks"), so the two backends
    stay in sync off a single value rather than a magic constant duplicated
    here. Missing/unparseable is a hard error per project rule 1 — a wrong
    looks count skews the cost model silently.
    """
    conf = os.path.join(sharedir, 'snaphu', 'config', 'snaphu.conf.brief')
    if not os.path.isfile(conf):
        sys.exit(f'UNWRAP: ERROR: cannot read NCORRLOOKS for whirlwind '
                 f'--nlooks: {conf} is missing')
    with open(conf) as fh:
        for line in fh:
            m = re.match(r'\s*NCORRLOOKS\s+([0-9.eE+-]+)', line)
            if m:
                return float(m.group(1))
    sys.exit(f'UNWRAP: ERROR: no NCORRLOOKS line in {conf}; cannot set '
             'whirlwind --nlooks (override with GMTSAR_WHIRLWIND_NLOOKS)')


def _whirlwind_argv(*, phase_in, corr_in, width, unwrap_out, conncomp_out,
                    defomax, sharedir):
    """Build the whirlwind command line. Split out so it is unit-testable."""
    nlooks_env = os.environ.get('GMTSAR_WHIRLWIND_NLOOKS')
    nlooks = float(nlooks_env) if nlooks_env else _ncorrlooks(sharedir)

    if float(defomax) != 0:
        # Difference 1 in the module docstring. Announced, not swallowed:
        # a config asking for phase jumps gets a solver that has no such
        # notion, and the operator needs to see that in the log.
        print(f'WHIRLWIND: NOTE: defomax={defomax} is a snaphu DEFOMAX_CYCLE '
              'setting with no whirlwind equivalent (MCF has no '
              'maximum-discontinuity ceiling); ignoring it.', file=sys.stderr)

    argv = [
        _whirlwind_bin(),
        '--phase', str(phase_in),        # flat float32, snaphu FLOAT_DATA
        '--cor', str(corr_in),           # flat float32, already threshold-masked
        '--cols', str(width),            # snaphu's "line length"
        '--nlooks', repr(nlooks),
        '--out', str(unwrap_out),
        '--out-format', 'float',         # flat float32 -> gmt xyz2grd -ZTLf
        '--conncomp', str(conncomp_out),  # non-TIFF path -> uint8 -> -ZTLu
    ]
    argv += shlex.split(os.environ.get('GMTSAR_WHIRLWIND_ARGS', ''))
    return argv


def _run_whirlwind(*, phase_in, corr_in, width, unwrap_out, conncomp_out,
                   defomax, sharedir):
    print(f'WHIRLWIND: using {whirlwind_version()}')
    argv = _whirlwind_argv(phase_in=phase_in, corr_in=corr_in, width=width,
                           unwrap_out=unwrap_out, conncomp_out=conncomp_out,
                           defomax=defomax, sharedir=sharedir)
    run(' '.join(shlex.quote(a) for a in argv))

    # The GMT side downstream reads these back by size (rows = bytes / 4 /
    # width). A short write would silently reshape the grid, so check here.
    ncols = int(width)
    for path, itemsize in ((unwrap_out, 4), (conncomp_out, 1)):
        assert os.path.isfile(path), f'WHIRLWIND: {path} was not written'
        nbytes = os.path.getsize(path)
        assert nbytes % (ncols * itemsize) == 0, (
            f'WHIRLWIND: {path} is {nbytes} bytes, not a whole number of '
            f'{ncols}-column rows at {itemsize} byte(s)/px')

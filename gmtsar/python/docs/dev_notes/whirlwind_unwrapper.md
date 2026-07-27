# whirlwind as an alternative phase unwrapper

Status: **mockup on branch `whirlwind-unwrapper`.** Wired and unit-tested; not
validated on a real scene, not run through the regression sweep, default OFF.

[whirlwind-insar](https://github.com/scottstanie/whirlwind-insar) is an
MCF-based InSAR phase unwrapper reported ~10x faster than SNAPHU. This note
records how it was slotted into GMTSAR alongside `snaphu`, what carries over
between the two, and what does not.

## The seam

GMTSAR calls the `snaphu` C binary at exactly one point, in two places that
mirror each other (`gmtsar/csh/snaphu{,_interp}.csh` and their Python port
`gmtsar/python/utils/snaphu.py`). That call is bracketed by pure GMT work.
Everything before it produces three things:

| name        | format                                            |
|-------------|---------------------------------------------------|
| `phase.in`  | flat float32, native endian, row-major (snaphu `FLOAT_DATA`) |
| `corr.in`   | flat float32, coherence in [0,1], **0 where masked out** |
| width       | column count, `gmt grdinfo -C phase_patch.grd` field 10 |

and everything after it consumes two:

| name           | format                | read back by            |
|----------------|-----------------------|-------------------------|
| `unwrap.out`   | flat float32          | `gmt xyz2grd -ZTLf`     |
| `conncomp.out` | flat uint8            | `gmt xyz2grd -ZTLu`     |

whirlwind reads and writes precisely those layouts natively, so the swap needs
**no format shims** — the masking, landmask, `nearest_grid` fill, xyz
round-trip, plotting, and cleanup around it are untouched and backend-agnostic.

```
whirlwind --phase phase.in --cor corr.in --cols <W> --nlooks <NCORRLOOKS> \
    --out unwrap.out --out-format float --conncomp conncomp.out
```

Three flags do the work: `--out-format float` gives snaphu's `FLOAT_DATA`
output rather than the two-band `.unw` default; a **non-`.tif`** `--conncomp`
path makes whirlwind write one byte per pixel (the snaphu/isce2 convention,
which is what `-ZTLu` expects); `--cols` is snaphu's "line length".

## What was changed

Per `CLAUDE.md` rule 3, all working code is inside `gmtsar/python/`.

| file | change |
|---|---|
| `utils/unwrap_backend.py` | **new.** Backend registry, selection, whirlwind command construction, snaphu invocation moved here verbatim. |
| `utils/snaphu.py` | the one `snaphu ...` `run()` call in `_snaphu_run()` and in the legacy `snaphu()` CLI replaced by `run_unwrapper(...)`; `unwrapper=` kwarg added to `snaphu_unwrap` / `snaphu_interp_unwrap`. |
| `utils/p2p_stages.py` | `unwrapper` threaded through `P2P5Unwrap` → `_call_snaphu`. |
| `utils/p2p_processing` | reads optional `unwrapper` from `config.py` (via `getattr`, so pre-existing configs keep working); added to the P2P5 stage-cache key so switching backends invalidates a cached sentinel. |
| `utils/merge_unwrap_geocode_tops` | reads `unwrapper` from config, forwards it (S1 TOPS merge path). |
| `utils/pop_config` | emits `unwrapper = 'snaphu'` with a comment in generated configs. |
| `bin_py/tests/test_unwrap_backend.py` | **new.** 22 tests: selection precedence, hard-fail on typos, command construction, and an end-to-end run asserting the byte layouts above. |
| `docs/dev_notes/whirlwind_csh.patch` | the equivalent csh change, as a patch (see below). |

`utils/import_csh_config` was deliberately **not** touched: its job is fidelity
to a tarball's bundled csh config, which never contains `unwrapper`. The
`getattr` default covers configs it generates.

### The csh side

`gmtsar/csh/snaphu.csh` and `snaphu_interp.csh` are upstream files, so the
change lives as a patch instead of an edit:

```bash
git apply gmtsar/python/docs/dev_notes/whirlwind_csh.patch
```

It replaces the `# run snaphu` block in both scripts with an
`if ($GMTSAR_UNWRAPPER == ...)` switch. Env var rather than a new positional
argument, because a 4th argument would have to be threaded through
`p2p_processing.csh`, `intf_tops.csh`, `merge_unwrap_geocode_tops.csh`,
`intf_batch_ALOS2_SCAN.csh`, `p2p_ENVI.csh`, `p2p_ALOS2_SCAN_SLC.csh`, and
`MAI_processing.csh`, all of which call the scripts positionally. Verified:
`csh -n` clean on both files, correct command line and a non-zero exit on an
unknown backend name.

## Selection

Precedence: `config.py` `unwrapper` → `$GMTSAR_UNWRAPPER` → `snaphu`.

```python
# config.py
unwrapper = 'whirlwind'
```
```bash
GMTSAR_UNWRAPPER=whirlwind p2p_processing S1_TOPS master aligned config.py
```

An unrecognised name is a **hard error from both sources**, never a fallback to
snaphu (project rule 1) — a typo'd `whirlwid` would otherwise yield a
plausible-looking product from the wrong solver. Likewise, a missing
`whirlwind` binary aborts up front with an install hint rather than failing
mid-unwrap after the rest of the pipeline has already run.

Env-only tuning knobs, so `config.py` stays backend-agnostic:

| var | default | meaning |
|---|---|---|
| `GMTSAR_WHIRLWIND_BIN` | `whirlwind` | executable name/path |
| `GMTSAR_WHIRLWIND_NLOOKS` | `NCORRLOOKS` from `snaphu.conf.brief` | `--nlooks` |
| `GMTSAR_WHIRLWIND_ARGS` | — | extra flags, appended last so they win |

## Differences that matter

1. **`defomax` is snaphu-only.** GMTSAR maps `defomax > 0` onto snaphu's DEFO
   statistical-cost mode with `DEFOMAX_CYCLE`; whirlwind's MCF solver has no
   maximum-discontinuity ceiling. The value is printed to stderr and ignored —
   announced, not swallowed. **This is the biggest open question for
   earthquake configs**, which are exactly the ones that set it.
2. **Only `NCORRLOOKS` carries over** from `snaphu.conf.brief`, as `--nlooks`
   (same role in both cost models). The whole SAR-geometry block —
   `ORBITRADIUS`, `BASELINE`, `NEARRANGE`, `DR`/`DA`, `LAMBDA`, `MAXFLOW`,
   `COSTSCALE` — is unused. Reading it from the file rather than hardcoding
   23.8 keeps the two backends off one number.
3. **whirlwind NaNs the background**, snaphu does not. Verified: NaN lands
   exactly where `conncomp == 0`. Mostly absorbed downstream, since
   `utils/snaphu.py` immediately multiplies by `mask2_patch.grd`, which is
   already NaN below `threshold_snaphu`. **Not** absorbed where whirlwind's own
   component rules drop a pixel that passed GMTSAR's threshold — those become
   extra NaNs in `unwrap.grd`. `GMTSAR_WHIRLWIND_ARGS="--conncomp-reliability 0"`
   labels every unwrapped pixel for snaphu-like coverage.
4. **`--mask` is not used.** It takes a TIFF only, so `mask2_patch.grd` can't
   be passed as-is. Masking still reaches the solver through `corr.in`, which
   the callers have already zeroed below `threshold_snaphu`, and whirlwind
   treats zero coherence as background (verified). Worth revisiting: the
   whirlwind docs call an explicit mask "critical for large real scenes with
   water/shadow, where the unmasked path can slow down by 10-100x" — so
   GMTSAR's landmask may be leaving speed on the table.
5. **Output is not bit-comparable** with snaphu and never will be; these are
   different algorithms. `tests/compare.py`'s py-vs-csh byte-identity check is
   meaningless across backends — a whirlwind sweep needs the SSIM/RMS path
   against a snaphu reference, not a diff.

## Before this can be more than a mockup

- [ ] Run `ALOS_haiti` (the only sweep case with `threshold_snaphu > 0`, hence
      the only one that exercises this code at all) both ways and compare
      products + wall time. Rule 12: pass/fail, perf table, and a
      `tools/py_vs_csh_figure.py` plot — not the SSIM number alone.
- [ ] Decide what `defomax > 0` should do under whirlwind: ignore (current),
      hard-error, or auto-fall-back to snaphu. Current behaviour is the least
      surprising for a mockup but the most surprising for a real deformation
      run.
- [ ] Quantify difference 3 on real data — how many pixels does whirlwind drop
      that GMTSAR's threshold kept?
- [ ] Decide packaging. The binary is a release download, not a build
      dependency; `install.py` does not fetch it, and nothing should default to
      whirlwind until it does.
- [ ] Check the SBAS/time-series path, which consumes `unwrap.grd` +
      `conncomp.grd` and was not examined here.

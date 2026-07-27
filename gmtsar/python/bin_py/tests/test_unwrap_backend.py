"""test_unwrap_backend — unit + end-to-end tests for utils/unwrap_backend.py.

Covers the three things that can silently produce a wrong product:

  1. Backend selection precedence (config arg > $GMTSAR_UNWRAPPER > snaphu)
     and the hard error on an unknown name — a typo must not fall back to
     snaphu (project rule 1).
  2. The whirlwind command line: the flags that make its I/O layout match
     snaphu's (`--out-format float`, flat `--conncomp` path, `--cols`), and
     `--nlooks` tracking NCORRLOOKS from snaphu.conf.brief.
  3. An end-to-end unwrap on synthetic flat-binary data, asserting the two
     output files are exactly the sizes the downstream `gmt xyz2grd -ZTLf`
     / `-ZTLu` calls in utils/snaphu.py assume (float32 and uint8, W columns).

Test 3 needs the `whirlwind` binary and skips without it; 1 and 2 are pure.
"""
import os
import shutil
import subprocess
import sys
import textwrap

import numpy as np
import pytest

_UTILS = os.path.join(os.path.dirname(__file__), "..", "..", "utils")
sys.path.insert(0, os.path.abspath(_UTILS))

import unwrap_backend as ub  # noqa: E402


# --- 1. backend selection --------------------------------------------------

def test_default_is_snaphu(monkeypatch):
    monkeypatch.delenv("GMTSAR_UNWRAPPER", raising=False)
    assert ub.select_backend(None) == "snaphu"


def test_env_selects_backend(monkeypatch):
    monkeypatch.setenv("GMTSAR_UNWRAPPER", "whirlwind")
    assert ub.select_backend(None) == "whirlwind"


def test_config_arg_beats_env(monkeypatch):
    monkeypatch.setenv("GMTSAR_UNWRAPPER", "whirlwind")
    assert ub.select_backend("snaphu") == "snaphu"


@pytest.mark.parametrize("unset", [None, "", -999, "-999"])
def test_unset_sentinels_defer_to_env(monkeypatch, unset):
    """GMTSAR's -999 "parameter not given" sentinel, and empty csh values,
    must both mean "defer", not "backend named -999"."""
    monkeypatch.setenv("GMTSAR_UNWRAPPER", "whirlwind")
    assert ub.select_backend(unset) == "whirlwind"


def test_case_and_whitespace_insensitive(monkeypatch):
    monkeypatch.delenv("GMTSAR_UNWRAPPER", raising=False)
    assert ub.select_backend(" Whirlwind ") == "whirlwind"


def test_unknown_backend_exits_not_falls_back(monkeypatch):
    """Rule 1: a typo'd backend must abort, never silently run snaphu."""
    monkeypatch.delenv("GMTSAR_UNWRAPPER", raising=False)
    with pytest.raises(SystemExit) as exc:
        ub.select_backend("whirlwid")
    assert "whirlwid" in str(exc.value)


def test_unknown_env_backend_also_exits(monkeypatch):
    monkeypatch.setenv("GMTSAR_UNWRAPPER", "snafu")
    with pytest.raises(SystemExit):
        ub.select_backend(None)


# --- 2. whirlwind command line --------------------------------------------

@pytest.fixture
def sharedir(tmp_path):
    """A minimal GMTSAR sharedir holding just the NCORRLOOKS line we read."""
    conf_dir = tmp_path / "snaphu" / "config"
    conf_dir.mkdir(parents=True)
    (conf_dir / "snaphu.conf.brief").write_text(textwrap.dedent("""\
        # trimmed copy of the bundled config
        INFILEFORMAT		FLOAT_DATA
        NCORRLOOKS	23.8
        DEFOMAX_CYCLE  65
    """))
    return str(tmp_path)


def _argv(sharedir, **kw):
    kw.setdefault("phase_in", "phase.in")
    kw.setdefault("corr_in", "corr.in")
    kw.setdefault("width", "840")
    kw.setdefault("unwrap_out", "unwrap.out")
    kw.setdefault("conncomp_out", "conncomp.out")
    kw.setdefault("defomax", 0)
    return ub._whirlwind_argv(sharedir=sharedir, **kw)


def test_argv_io_layout_matches_snaphu(monkeypatch, sharedir):
    """These four flags are what let the surrounding GMT plumbing stay
    untouched: flat float32 out, flat uint8 conncomp, snaphu's line length."""
    monkeypatch.delenv("GMTSAR_WHIRLWIND_ARGS", raising=False)
    monkeypatch.delenv("GMTSAR_WHIRLWIND_NLOOKS", raising=False)
    argv = _argv(sharedir)
    assert argv[argv.index("--out-format") + 1] == "float"
    assert argv[argv.index("--cols") + 1] == "840"
    assert argv[argv.index("--phase") + 1] == "phase.in"
    assert argv[argv.index("--cor") + 1] == "corr.in"
    # A non-.tif conncomp path is what makes whirlwind write 1 byte/px.
    cc = argv[argv.index("--conncomp") + 1]
    assert cc == "conncomp.out" and not cc.endswith((".tif", ".tiff"))


def test_nlooks_tracks_ncorrlooks(monkeypatch, sharedir):
    monkeypatch.delenv("GMTSAR_WHIRLWIND_NLOOKS", raising=False)
    argv = _argv(sharedir)
    assert float(argv[argv.index("--nlooks") + 1]) == 23.8


def test_nlooks_env_override(monkeypatch, sharedir):
    monkeypatch.setenv("GMTSAR_WHIRLWIND_NLOOKS", "4")
    argv = _argv(sharedir)
    assert float(argv[argv.index("--nlooks") + 1]) == 4.0


def test_missing_ncorrlooks_is_fatal(monkeypatch, tmp_path):
    """Rule 1: no silent default looks count — it skews the cost model."""
    monkeypatch.delenv("GMTSAR_WHIRLWIND_NLOOKS", raising=False)
    conf_dir = tmp_path / "snaphu" / "config"
    conf_dir.mkdir(parents=True)
    (conf_dir / "snaphu.conf.brief").write_text("INFILEFORMAT FLOAT_DATA\n")
    with pytest.raises(SystemExit):
        _argv(str(tmp_path))


def test_extra_args_appended_last(monkeypatch, sharedir):
    """GMTSAR_WHIRLWIND_ARGS must come last so an operator can override a
    default we set (clap takes the last occurrence)."""
    monkeypatch.setenv("GMTSAR_WHIRLWIND_ARGS",
                       "--goldstein-alpha 0.7 --nlooks 9")
    argv = _argv(sharedir)
    assert argv[-4:] == ["--goldstein-alpha", "0.7", "--nlooks", "9"]


def test_defomax_is_announced_not_swallowed(monkeypatch, sharedir, capsys):
    """defomax has no whirlwind equivalent. It must be visible in the log,
    not silently dropped, so an earthquake config doesn't quietly lose its
    phase-jump setting."""
    monkeypatch.delenv("GMTSAR_WHIRLWIND_ARGS", raising=False)
    _argv(sharedir, defomax=65)
    err = capsys.readouterr().err
    assert "defomax=65" in err and "whirlwind" in err.lower()


def test_binary_override(monkeypatch, sharedir):
    monkeypatch.setenv("GMTSAR_WHIRLWIND_BIN", "/opt/whirlwind/bin/whirlwind")
    assert _argv(sharedir)[0] == "/opt/whirlwind/bin/whirlwind"


def test_missing_binary_is_fatal_with_install_hint(monkeypatch):
    monkeypatch.setenv("GMTSAR_WHIRLWIND_BIN", "whirlwind-does-not-exist")
    with pytest.raises(SystemExit) as exc:
        ub.whirlwind_version()
    assert "releases" in str(exc.value)


# --- 3. end-to-end I/O contract -------------------------------------------

@pytest.mark.skipif(shutil.which("whirlwind") is None,
                    reason="whirlwind binary not on PATH")
def test_end_to_end_output_shapes(tmp_path, sharedir, monkeypatch):
    """Run the real binary on snaphu-format inputs and assert the outputs are
    exactly what utils/snaphu.py's xyz2grd calls will read back.

    A ramp plus noise, with the top 20 rows zeroed in coherence to stand in
    for GMTSAR's below-threshold mask.
    """
    monkeypatch.delenv("GMTSAR_WHIRLWIND_ARGS", raising=False)
    monkeypatch.delenv("GMTSAR_WHIRLWIND_NLOOKS", raising=False)
    monkeypatch.chdir(tmp_path)

    W, H = 200, 150
    rng = np.random.default_rng(0)
    y, x = np.mgrid[0:H, 0:W]
    ifg = np.exp(1j * (0.05 * x + 0.03 * y)) + 0.3 * (
        rng.standard_normal((H, W)) + 1j * rng.standard_normal((H, W)))
    np.angle(ifg).astype("float32").tofile("phase.in")

    cor = (rng.random((H, W)) * 0.5 + 0.5).astype("float32")
    cor[:20, :] = 0.0
    cor.tofile("corr.in")

    ub.run_unwrapper("whirlwind", phase_in="phase.in", corr_in="corr.in",
                     width=W, unwrap_out="unwrap.out",
                     conncomp_out="conncomp.out", defomax=0,
                     sharedir=sharedir)

    # float32, one value per pixel -> `gmt xyz2grd -ZTLf`
    assert os.path.getsize("unwrap.out") == W * H * 4
    # uint8, one value per pixel -> `gmt xyz2grd -ZTLu`
    assert os.path.getsize("conncomp.out") == W * H

    unw = np.fromfile("unwrap.out", dtype="float32").reshape(H, W)
    cc = np.fromfile("conncomp.out", dtype="uint8").reshape(H, W)

    # The zeroed-coherence band must land in background component 0, the same
    # region GMTSAR's mask2_patch.grd will NaN out downstream.
    assert (cc[:20, :] == 0).all()
    assert (cc[20:, :] > 0).any()

    # Difference 5 in the unwrap_backend docstring: whirlwind NaNs the
    # background, snaphu does not. Pin the exact correspondence, since the
    # downstream `MUL mask2_patch.grd` only absorbs it where the two masks
    # agree — a change here means extra NaNs appearing in unwrap.grd.
    assert np.array_equal(~np.isfinite(unw), cc == 0)
    assert np.isfinite(unw[cc > 0]).all()


@pytest.mark.skipif(shutil.which("whirlwind") is None,
                    reason="whirlwind binary not on PATH")
def test_version_is_reported():
    v = ub.whirlwind_version()
    assert v.startswith("whirlwind")


# --- 4. wiring -------------------------------------------------------------

def test_snaphu_py_threads_unwrapper_through():
    """utils/snaphu.py must accept and forward the `unwrapper` kwarg, or the
    config knob silently does nothing."""
    import inspect
    import importlib
    snaphu = importlib.import_module("snaphu")
    for fn in (snaphu.snaphu_unwrap, snaphu.snaphu_interp_unwrap):
        assert "unwrapper" in inspect.signature(fn).parameters


def test_pop_config_emits_unwrapper(tmp_path):
    """A generated config.py should advertise the option.

    Run in tmp_path: pop_config writes config.py into the cwd, and this test
    used to drop one into utils/ as a side effect.
    """
    pop = os.path.abspath(os.path.join(_UTILS, "pop_config"))
    subprocess.run([sys.executable, pop, "ALOS"], cwd=tmp_path, check=True,
                   stdout=subprocess.DEVNULL)
    generated = tmp_path / "config.py"
    assert generated.is_file(), "pop_config did not write config.py"
    assert "unwrapper" in generated.read_text()

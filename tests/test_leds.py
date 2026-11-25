import pytest

from zozobox.config import Btn, LedConfig
from zozobox.hw.pi import mode2_value
from zozobox.leds import DUTY_MAX, FakeLedBackend, LedController, LedState, level_for, to_duty

CFG = LedConfig()


def ctl(t0=0.0):
    t = [t0]
    be = FakeLedBackend()
    c = LedController(CFG, be, clock=lambda: t[0], start_thread=False)
    return c, be, t


def test_static_levels():
    assert level_for(LedState.OFF, 5, 0, CFG) == 0
    assert level_for(LedState.DIM, 5, 0, CFG) == CFG.dim_level
    assert level_for(LedState.BRIGHT, 5, 0, CFG) == 1


@pytest.mark.parametrize("state,period", [(LedState.PULSE_SLOW, CFG.slow_pulse_s), (LedState.PULSE_FAST, CFG.fast_pulse_s)])
def test_pulse_starts_dim_peaks_mid_period(state, period):
    assert level_for(state, 10, 10, CFG) == pytest.approx(CFG.dim_level)
    assert level_for(state, 10 + period / 2, 10, CFG) == pytest.approx(1.0)
    assert level_for(state, 10 + period, 10, CFG) == pytest.approx(CFG.dim_level)


def test_duty_gamma_and_clamp():
    assert to_duty(0, CFG) == 0
    assert to_duty(1, CFG) == DUTY_MAX
    assert to_duty(2, CFG) == DUTY_MAX
    assert 0 < to_duty(0.5, CFG) < DUTY_MAX // 2  # gamma pulls mid-levels down
    assert to_duty(1, CFG, trim=0.5) < DUTY_MAX


def test_starts_off_and_writes_all_channels():
    c, be, _ = ctl()
    assert be.duty == {0: 0, 1: 0, 2: 0, 3: 0}


def test_only_changed_channels_are_written():
    c, be, t = ctl()
    n = be.writes
    c.render()
    assert be.writes == n
    c.set(Btn.BLUE, LedState.BRIGHT)
    c.render()
    assert be.writes == n + 1
    assert be.duty[CFG.channels[Btn.BLUE]] == DUTY_MAX


def test_pulse_animates_over_time():
    c, be, t = ctl()
    c.set(Btn.RED, LedState.PULSE_SLOW)
    seen = set()
    for i in range(20):
        t[0] = i * 0.1
        c.render()
        seen.add(be.duty[0])
    assert len(seen) > 5


def test_i2c_error_is_retried_next_frame():
    c, be, t = ctl()
    c.set(Btn.GREEN, LedState.BRIGHT)
    be.fail_next = 1
    c.render()  # write fails, logged
    assert be.duty[1] == 0
    c.render()
    assert be.duty[1] == DUTY_MAX


def test_close_turns_everything_off():
    c, be, _ = ctl()
    c.set_all(LedState.BRIGHT)
    c.render()
    c.close()
    assert set(be.duty.values()) == {0}
    assert be.closed


def test_mode2_open_drain_inverted():
    assert mode2_value(open_drain=True, invert=True) == 0x10
    assert mode2_value(open_drain=False, invert=False) == 0x04



def test_ensure_lg_wd_points_lgpio_at_a_writable_dir(tmp_path, monkeypatch):
    import os

    import zozobox.hw.pi as pi

    home = tmp_path / "home"   # the admin's 0700 home, unwritable for zozobox
    data = tmp_path / "data"
    home.mkdir()
    data.mkdir()
    real = os.access
    monkeypatch.setattr(pi.os, "access",
                        lambda p, m: False if str(p) == str(home) else real(p, m))
    env = {"ZOZOBOX_DATA_DIR": str(data)}
    assert pi.ensure_lg_wd(str(home), env) == str(data) and env["LG_WD"] == str(data)
    env = {}
    assert pi.ensure_lg_wd(str(home), env) == env["LG_WD"]  # falls back to the temp dir
    assert pi.ensure_lg_wd(str(tmp_path), {}) is None  # writable cwd: leave it alone
    env = {"LG_WD": "/somewhere"}
    assert pi.ensure_lg_wd(str(home), env) is None and env["LG_WD"] == "/somewhere"

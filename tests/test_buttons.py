from zozobox.buttons import ButtonArbiter, ButtonInput, FakeButtonBackend, ParentChord, Press
from zozobox.config import Btn, ButtonConfig

CFG = ButtonConfig()
R, G, B, Y = Btn.RED, Btn.GREEN, Btn.BLUE, Btn.YELLOW


class Pins:
    def __init__(self):
        self.level = {b: False for b in CFG.pins}

    def __call__(self, b):
        return self.level[b]


def arb():
    pins = Pins()
    return ButtonArbiter(CFG, pins), pins


def presses(events):
    return [e for e in events if isinstance(e, Press)]


def test_single_press_accepted():
    a, _ = arb()
    assert a.edge(R, True, 10.0) == [Press(R, 10.0)]


def test_bounce_is_filtered():
    a, pins = arb()
    ev = a.edge(R, True, 10.0)
    pins.level[R] = True
    ev += a.edge(R, False, 10.003)
    ev += a.edge(R, True, 10.006)
    ev += a.tick(10.03)
    assert presses(ev) == [Press(R, 10.0)]
    assert a.is_down(R)


def test_short_tap_inside_debounce_is_reconciled():
    # Press, then a real release within the debounce window: no later edges.
    a, pins = arb()
    a.edge(R, True, 10.0)
    a.edge(R, False, 10.010)  # ignored by debounce
    pins.level[R] = False
    a.tick(10.025)
    assert not a.is_down(R)  # not stuck down
    # And the next real press works.
    assert presses(a.edge(R, True, 12.0)) == [Press(R, 12.0)]


def test_lockout_drops_other_buttons_and_repeats():
    a, _ = arb()
    ev = a.edge(R, True, 10.0)
    a.edge(R, False, 10.1)
    ev += a.edge(G, True, 10.3)  # dropped
    a.edge(G, False, 10.4)
    ev += a.edge(R, True, 10.6)  # dropped
    a.edge(R, False, 10.7)
    ev += a.edge(B, True, 11.05)  # accepted
    assert presses(ev) == [Press(R, 10.0), Press(B, 11.05)]


def test_dropped_presses_are_not_queued():
    a, _ = arb()
    a.edge(R, True, 10.0)
    a.edge(G, True, 10.2)  # dropped, held down past lockout
    assert presses(a.tick(11.5)) == []


def test_simultaneous_slap_first_wins():
    a, _ = arb()
    ev = a.edge(Y, True, 10.0) + a.edge(B, True, 10.001) + a.edge(G, True, 10.002)
    assert presses(ev) == [Press(Y, 10.0)]


def test_chord_fires_once_after_hold():
    a, _ = arb()
    a.edge(R, True, 10.0)
    a.edge(Y, True, 10.2)
    assert not [e for e in a.tick(15.1) if isinstance(e, ParentChord)]
    assert a.tick(15.2) == [ParentChord(15.2)]
    assert a.tick(20.0) == []  # only once per hold


def test_chord_cancelled_by_release():
    a, _ = arb()
    a.edge(R, True, 10.0)
    a.edge(Y, True, 10.2)
    a.edge(Y, False, 12.0)
    assert a.tick(16.0) == []


def test_chord_can_fire_again_after_release():
    a, _ = arb()
    a.edge(R, True, 0.0)
    a.edge(Y, True, 0.0 + 0.5)
    assert a.tick(5.6) == [ParentChord(5.6)]
    a.edge(Y, False, 6.0)
    a.edge(Y, True, 7.0)
    assert a.tick(12.1) == [ParentChord(12.1)]


def test_button_input_publishes_events_with_fake_backend():
    t = [100.0]
    be = FakeButtonBackend(CFG.pins)
    bi = ButtonInput(CFG, be, clock=lambda: t[0], start_thread=False)
    be.set(G, True)
    assert bi.events.get_nowait() == Press(G, 100.0)
    t[0] = 100.005
    be.set(G, False)  # inside debounce: ignored, then reconciled by poll
    t[0] = 100.03
    bi.poll()
    assert bi.events.empty()
    assert not bi._arbiter.is_down(G)
    bi.close()

import pytest

from zozobox.mood import has_speech, keyword_match


@pytest.mark.parametrize("text,label", [
    ("I happy", "happy"),
    ("happy!", "happy"),
    ("Happy happy", "happy"),
    ("mad!", "mad"),
    ("I'm angry", "mad"),
    ("grumpy", "mad"),
    ("sad sad", "sad"),
    ("Sad.", "sad"),
    ("okay", "neutral"),
    ("OK", "neutral"),
    ("I'm fine", "neutral"),
    ("so-so", "neutral"),
    ("yay!", "happy"),
])
def test_unambiguous(text, label):
    assert keyword_match(text) == label


@pytest.mark.parametrize("text", [
    "", "   ", "...", None,
    "not happy",            # negated
    "no mad",
    "I don't sad",
    "happy and sad",        # two moods
    "mad okay",
    "good",                 # ambiguous word, left to Claude
    "doggy",                # no mood word
    "I was sad at the park but now I am very happy",  # long
    "I'm happy I'm happy I'm happy I'm happy",   # > MAX_WORDS
])
def test_ambiguous_goes_to_model(text):
    assert keyword_match(text) is None


def test_has_speech():
    assert has_speech("ok")
    assert not has_speech("")
    assert not has_speech(" ... !? ")
    assert not has_speech(None)

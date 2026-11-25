"""Bundled, pre-vetted evergreen content (SPEC 7.1 fallback) and fixed phrases.

Texts here are written by hand and reviewed by people, so they skip the model
text review. `bootstrap` turns them into audio and images once (images still
go through the image safety check), stored under `media/evergreen/`. Served
copies still follow the store's first-month parent-approval gate (SPEC 7.1). When generation fails twice, or the safety gate rejects
both attempts, the pipeline serves a copy of one of these.
"""

from __future__ import annotations

from dataclasses import dataclass

# -- fixed phrases (zozobox.engine.sources.PHRASES) ---------------------------

PHRASE_TEXTS: dict[str, str] = {
    "say_hi_grandma": "Say hi to Grandma!",
    "sleepy": "Shh, the box is sleeping. Night night!",
    "printer_sleepy": "Oh! The printer is sleepy right now. Let's try again later.",
    "no_story_yet": "Your story is still being made. Come back soon!",
    "print_not_ready": "That one is not ready yet. Let's try the next one!",
    "ask_mood": "How are you feeling today?",
    "ask_mood_again": "Can you tell me one more time? How do you feel?",
    "you_feel_happy": "You feel happy! Yay!",
    "you_feel_mad": "You feel mad. That's okay. Let's take a big breath together.",
    "you_feel_sad": "You feel sad. That's okay. A big hug for you.",
    "you_feel_neutral": "You feel okay. Thank you for telling me!",
    "all_printed_today": "You got all your cards today! More tomorrow!",
    "here_is_your_card": "Here is your card!",
    "praise_great_job": "Great job!",
    "praise_well_done": "Well done!",
    "praise_you_did_it": "You did it!",
}


# -- stories -------------------------------------------------------------------

@dataclass(frozen=True)
class EvergreenStory:
    slug: str
    title: str
    value: str
    chunks: tuple[str, ...]

    @property
    def text(self) -> str:
        return " ".join(self.chunks)


STORIES: tuple[EvergreenStory, ...] = (
    EvergreenStory(
        "garden-helper", "Zozo the Garden Helper", "helping",
        (
            "One sunny morning, Zozo put on little green boots. Today was garden day! "
            "Grandpa had a watering can, and Zozo had a tiny one too.",
            "Splish, splash! Zozo watered the yellow flowers. Splish, splash! Zozo watered "
            "the round red tomatoes. A little ladybug said hello from a leaf.",
            "Then Zozo helped carry the basket. It was a bit heavy, so Zozo tried again, "
            "with two hands this time. Up it went! Grandpa clapped. You are a great helper!",
            "The flowers stood tall and happy. Zozo gave them a little wave. Thank you, "
            "garden, said Zozo. And the sun smiled down on everyone.",
        ),
    ),
    EvergreenStory(
        "rainy-day-tower", "Zozo Builds a Tower", "trying again",
        (
            "Pitter, patter went the rain on the window. Zozo was cozy inside with a big "
            "box of blocks. Today, Zozo wanted to build a tall, tall tower.",
            "One block, two blocks, three blocks. Wobble, wobble, tumble! The blocks fell "
            "down. Oh! said Zozo. Then Zozo took a big breath and smiled.",
            "Let's try again! This time Zozo put the big blocks at the bottom and the little "
            "blocks on top. One, two, three, four, five! The tower stood up tall.",
            "Mommy came to see. Wow, Zozo, you kept trying! Zozo felt proud and happy. "
            "Outside, the rain stopped, and a rainbow said hello.",
        ),
    ),
    EvergreenStory(
        "duck-pond", "Zozo Shares at the Pond", "sharing",
        (
            "Zozo and Daddy walked to the pond. Zozo had a little bag of duck food. Quack, "
            "quack! Three ducks came paddling over to say hello.",
            "The big duck got some food. The middle duck got some food. But the tiny duck "
            "was all the way at the back. Zozo saw the tiny duck.",
            "Here you go, little duck! Zozo gently tossed some food right to the tiny duck. "
            "Quack! said the tiny duck, as if to say thank you.",
            "Now every duck had a snack. Sharing makes everyone happy, said Daddy. Zozo "
            "waved bye-bye to the ducks, and they all said quack, quack, quack.",
        ),
    ),
    EvergreenStory(
        "thank-you-day", "Zozo's Thank You Day", "gratitude",
        (
            "When Zozo woke up, the sun was peeking in. Good morning, sun! Thank you for "
            "the light, said Zozo with a big stretch and a yawn.",
            "At breakfast there were warm oats with banana. Thank you for breakfast, said "
            "Zozo. Mommy smiled a big smile.",
            "At the park, a friend let Zozo have a turn on the swing. Thank you, friend! "
            "Whee! Up and down, up and down went Zozo.",
            "At bedtime, Zozo hugged a soft teddy. Thank you for this happy day, Zozo "
            "whispered. And Zozo snuggled in, warm and cozy and glad.",
        ),
    ),
)


# -- coloring themes -------------------------------------------------------------

@dataclass(frozen=True)
class EvergreenColoring:
    slug: str
    title: str
    subject: str
    intro: str


COLORING: tuple[EvergreenColoring, ...] = (
    EvergreenColoring("happy-turtle", "Happy Turtle", "a smiling turtle sitting on a small rock",
                      "Here is a happy turtle for you to color!"),
    EvergreenColoring("big-apple", "Big Apple", "one big apple with a leaf on top",
                      "Here is a big round apple for you to color!"),
    EvergreenColoring("little-train", "Little Train", "a friendly little toy train with round wheels",
                      "Choo choo! Here is a little train for you to color!"),
    EvergreenColoring("sunny-flower", "Sunny Flower", "one big flower with round petals and two leaves",
                      "Here is a pretty flower for you to color!"),
)


# -- letters -------------------------------------------------------------------

LETTER_SOUNDS: dict[str, str] = {
    "A": "ah", "B": "buh", "C": "kuh", "D": "duh", "E": "eh", "F": "fff", "G": "guh",
    "H": "huh", "I": "ih", "J": "juh", "K": "kuh", "L": "lll", "M": "mmm", "N": "nnn",
    "O": "oh", "P": "puh", "Q": "kwuh", "R": "rrr", "S": "sss", "T": "tuh", "U": "uh",
    "V": "vvv", "W": "wuh", "X": "ks", "Y": "yuh", "Z": "zzz",
}

# Hand-picked, concrete, drawable, cheerful words (first is the drawing).
LETTER_WORDS: dict[str, tuple[str, ...]] = {
    "A": ("apple", "ant", "alligator"),
    "B": ("ball", "bear", "banana", "boat"),
    "C": ("cat", "car", "cup", "cookie"),
    "D": ("duck", "dog", "drum"),
    "E": ("egg", "elephant", "elbow"),
    "F": ("fish", "flower", "frog"),
    "G": ("goat", "grass", "gift"),
    "H": ("hat", "house", "horse"),
    "I": ("igloo", "ice cream", "insect"),
    "J": ("jar", "jam", "jellyfish"),
    "K": ("kite", "key", "kitten"),
    "L": ("leaf", "lamp", "lion"),
    "M": ("moon", "milk", "mouse"),
    "N": ("nest", "nose", "net"),
    "O": ("owl", "orange", "octopus"),
    "P": ("pig", "pear", "puppy"),
    "Q": ("queen", "quilt", "quack"),
    "R": ("rabbit", "rainbow", "rocket"),
    "S": ("sun", "sock", "star"),
    "T": ("tree", "truck", "turtle"),
    "U": ("umbrella", "up", "unicorn"),
    "V": ("van", "violin", "vase"),
    "W": ("whale", "wagon", "water"),
    "X": ("xylophone", "x-ray"),
    "Y": ("yo-yo", "yak", "yogurt"),
    "Z": ("zebra", "zoo", "zipper"),
}

LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def letter_intro(letter: str) -> str:
    L = letter.upper()
    return f"Today's letter is {L}! {L} says {LETTER_SOUNDS.get(L, L.lower())}!"


def word_prompt_text(letter: str, word: str) -> str:
    return f"{letter.upper()}... {word}! Can you say {word}?"

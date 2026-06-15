"""
Lightweight tests for the turn-boundary cue detector (no external dependencies).
These tests duplicate the small _text_has_turn_cue predicate so they run
without pydantic / piper / faster-whisper installed.
"""

import unittest


def _text_has_turn_cue(text: str) -> bool:
    """Minimal copy of the production logic for isolated testing."""
    if not text:
        return False
    t = text.lower().strip().rstrip(".!?,")

    if t == "over" or t.endswith(" over"):
        return True

    turn_cues = {
        "right?", "yeah?", "yes?", "no?", "correct?", "true?",
        "okay?", "ok?", "kay?", "alright?", "all right?",
        "got it?", "clear?", "see?", "understand?", "follow?",
        "cool?", "good?", "fair?", "sound good?", "deal?",
        "you know?", "y'know?", "ya know?",
        "know what i mean?", "know what i'm saying?", "know'm sayin'?",
        "make sense?", "does that make sense?",
        "see what i mean?", "see what i'm saying?",
        "you with me?", "you feel me?", "feel me?",
        "am i right?", "isn't that right?",
        "wouldn't you say?", "don't you think?",
        "or what?", "or no?", "fair enough?",
        "what do you think?", "thoughts?", "your thoughts?",
        "any questions?", "sound good to you?", "works for you?", "up to you",
        "innit?", "eh?", "huh?",
        "you heard?",
        "that's it", "i'm done", "end of story", "period",
        "that's all", "moving on",
        "well", "hmm",
    }
    for cue in turn_cues:
        bare = cue.rstrip("?")
        if t.endswith(cue) or t.endswith(bare):
            return True
    return False


class TestTurnCues(unittest.TestCase):
    def test_over_cue(self):
        self.assertTrue(_text_has_turn_cue("Over"))
        self.assertTrue(_text_has_turn_cue("That's it, over."))
        self.assertFalse(_text_has_turn_cue("Over and out but not end"))

    def test_common_tag_questions(self):
        for cue in ["right?", "yeah?", "okay?", "got it?", "make sense?", "you know?"]:
            self.assertTrue(_text_has_turn_cue(f"Blah blah {cue}"), msg=cue)

    def test_well_and_hmm_style(self):
        self.assertTrue(_text_has_turn_cue("Well..."))
        self.assertTrue(_text_has_turn_cue("Hmm."))

    def test_no_false_positive(self):
        self.assertFalse(_text_has_turn_cue("Can you do it right now?"))
        self.assertFalse(_text_has_turn_cue("This is the right answer."))

    def test_case_and_punctuation_robust(self):
        self.assertTrue(_text_has_turn_cue("RIGHT?"))
        self.assertTrue(_text_has_turn_cue("Got it!"))


if __name__ == "__main__":
    unittest.main()
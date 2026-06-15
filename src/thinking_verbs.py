"""
Claude-style spinner verbs for long-wait voice acknowledgements.

Sourced from Anthropic's Claude Code thinking/spinner verb list (~187 verbs).
Phrases are present-participle + period for short Piper TTS cues.
"""

from typing import List

# Present participles shown while Claude / Claude Code is working.
CLAUDE_SPINNER_VERBS: tuple[str, ...] = (
    "Accomplishing",
    "Actioning",
    "Actualizing",
    "Architecting",
    "Baking",
    "Beaming",
    "Beboppin'",
    "Befuddling",
    "Billowing",
    "Blanching",
    "Bloviating",
    "Boogieing",
    "Boondoggling",
    "Booping",
    "Bootstrapping",
    "Brewing",
    "Bunning",
    "Burrowing",
    "Calculating",
    "Canoodling",
    "Caramelizing",
    "Cascading",
    "Catapulting",
    "Cerebrating",
    "Channeling",
    "Choreographing",
    "Churning",
    "Coalescing",
    "Cogitating",
    "Combobulating",
    "Composing",
    "Computing",
    "Concocting",
    "Considering",
    "Contemplating",
    "Cooking",
    "Crafting",
    "Creating",
    "Crunching",
    "Crystallizing",
    "Cultivating",
    "Deciphering",
    "Deliberating",
    "Determining",
    "Dilly-dallying",
    "Discombobulating",
    "Doing",
    "Doodling",
    "Drizzling",
    "Ebbing",
    "Effecting",
    "Elucidating",
    "Embellishing",
    "Enchanting",
    "Envisioning",
    "Evaporating",
    "Fermenting",
    "Fiddle-faddling",
    "Finagling",
    "Flambéing",
    "Flowing",
    "Flummoxing",
    "Fluttering",
    "Forging",
    "Forming",
    "Frolicking",
    "Frosting",
    "Gallivanting",
    "Galloping",
    "Garnishing",
    "Generating",
    "Gesticulating",
    "Germinating",
    "Grooving",
    "Gusting",
    "Harmonizing",
    "Hashing",
    "Hatching",
    "Herding",
    "Hullaballooing",
    "Hyperspacing",
    "Ideating",
    "Imagining",
    "Improvising",
    "Incubating",
    "Inferring",
    "Infusing",
    "Ionizing",
    "Jitterbugging",
    "Julienning",
    "Kneading",
    "Leavening",
    "Levitating",
    "Lollygagging",
    "Manifesting",
    "Marinating",
    "Meandering",
    "Metamorphosing",
    "Misting",
    "Moonwalking",
    "Moseying",
    "Mulling",
    "Mustering",
    "Musing",
    "Nebulizing",
    "Nesting",
    "Noodling",
    "Nucleating",
    "Orbiting",
    "Orchestrating",
    "Osmosing",
    "Perambulating",
    "Percolating",
    "Perusing",
    "Philosophising",
    "Photosynthesizing",
    "Pollinating",
    "Pondering",
    "Pontificating",
    "Pouncing",
    "Precipitating",
    "Processing",
    "Proofing",
    "Propagating",
    "Puttering",
    "Puzzling",
    "Recombobulating",
    "Reticulating",
    "Roosting",
    "Ruminating",
    "Sautéing",
    "Scampering",
    "Schlepping",
    "Scurrying",
    "Seasoning",
    "Shenaniganing",
    "Shimmying",
    "Simmering",
    "Skedaddling",
    "Sketching",
    "Slithering",
    "Smooshing",
    "Spelunking",
    "Spinning",
    "Sprouting",
    "Stewing",
    "Sublimating",
    "Swirling",
    "Swooping",
    "Symbioting",
    "Synthesizing",
    "Tempering",
    "Thinking",
    "Thundering",
    "Tinkering",
    "Tomfoolering",
    "Topsy-turvying",
    "Transfiguring",
    "Transmuting",
    "Twisting",
    "Undulating",
    "Unfurling",
    "Unravelling",
    "Vibing",
    "Waddling",
    "Wandering",
    "Warping",
    "Whirlpooling",
    "Whirring",
    "Whisking",
    "Wibbling",
    "Working",
    "Wrangling",
    "Zesting",
    "Zigzagging",
)

# Culled for voice: whimsical / culinary / locomotion — not computing or retrieval.
# Kept from that cut: Brewing, Concocting, Cooking, Stewing (still sound like "working on it").
SPINNER_VERBS_EXCLUDED: frozenset[str] = frozenset({
    # culinary (except cooking, stewing — kept in CLAUDE_SPINNER_VERBS)
    "baking", "blanching", "caramelizing", "drizzling", "fermenting", "flambéing",
    "frosting", "garnishing", "julienning", "kneading", "leavening", "marinating",
    "sautéing", "seasoning", "tempering", "whisking", "zesting",
    # dance / movement / goofing
    "beboppin'", "boogieing", "bunning", "canoodling", "frolicking", "gallivanting",
    "galloping", "gesticulating", "grooving", "jitterbugging", "lollygagging",
    "meandering", "moonwalking", "moseying", "perambulating", "scampering", "schlepping",
    "scurrying", "shimmying", "skedaddling", "slithering", "swooping", "waddling",
    "wandering", "zigzagging",
    # nature / weather / physical whimsy
    "beaming", "billowing", "burrowing", "catapulting", "ebbing", "enchanting",
    "evaporating", "flowing", "fluttering", "gusting", "harmonizing", "levitating",
    "manifesting", "misting", "orbiting", "photosynthesizing", "pollinating",
    "pouncing", "precipitating", "puttering", "roosting", "sprouting", "swirling",
    "thundering", "twisting", "undulating", "unfurling", "whirlpooling",
    # nonsense / unrelated
    "befuddling", "bloviating", "boondoggling", "booping", "choreographing",
    "dilly-dallying", "doodling", "fiddle-faddling", "flummoxing", "hullaballooing",
    "ionizing", "nebulizing", "shenaniganing", "smooshing", "spelunking", "symbioting",
    "tomfoolering", "topsy-turvying", "vibing", "wibbling", "finagling", "pontificating",
    # extra cuts — not retrieval/compute voice cues
    "philosophising", "tinkering", "unravelling", "wrangling",
})

# Voice-friendly extras (not in the leaked list but read clearly on the phone).
VOICE_SPINNER_EXTRAS: tuple[str, ...] = (
    "Retrieving",
    "Searching",
    "Looking",
    "Checking",
    "Fetching",
    "Analyzing",
    "Reviewing",
    "Investigating",
    "Stand by",
)

LONG_WAIT_APOLOGIES: tuple[str, ...] = (
    "Sorry for the wait.",
    "Sorry this is taking so long.",
    "Still working, sorry.",
    "Apologies, still on it.",
    "This is slower than it should be, sorry.",
    "Thanks for waiting, sorry.",
    "Sorry, almost there.",
    "Hang on, sorry about the delay.",
)

# "Sorry, still pondering." pairs for the post-apology phase.
_APOLOGY_VERB_STEMS: tuple[str, ...] = (
    "pondering",
    "processing",
    "retrieving",
    "searching",
    "thinking",
    "working",
    "crunching",
    "deliberating",
    "investigating",
    "checking",
    "fetching",
    "computing",
    "cogitating",
    "ruminating",
    "brewing",
    "cooking",
    "stewing",
    "simmering",
    "percolating",
    "concocting",
)


def spinner_verb_phrases() -> List[str]:
    """Single-word (or short) spinner cues for TTS."""
    phrases: List[str] = []
    seen: set[str] = set()
    for verb in (*CLAUDE_SPINNER_VERBS, *VOICE_SPINNER_EXTRAS):
        if verb.lower() in SPINNER_VERBS_EXCLUDED:
            continue
        text = verb if verb.endswith(".") else f"{verb}."
        key = text.lower()
        if key not in seen:
            seen.add(key)
            phrases.append(text)
    return phrases


def apology_phrase_variants() -> List[str]:
    """Standalone apologies plus 'Sorry, still …' combinations."""
    pool = list(LONG_WAIT_APOLOGIES)
    pool.extend(f"Sorry, still {stem}." for stem in _APOLOGY_VERB_STEMS)
    return pool


def long_wait_phrase_pool(elapsed_seconds: float, apology_after_seconds: float = 60.0) -> List[str]:
    """Phrases to choose from based on how long Hermes has been working."""
    pool = spinner_verb_phrases()
    if elapsed_seconds >= apology_after_seconds:
        pool = pool + apology_phrase_variants()
    return pool
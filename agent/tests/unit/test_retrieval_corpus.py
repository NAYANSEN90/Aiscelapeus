"""Retrieval correctness as a blocking gate -- RET-C03 and RET-C04.

BUILD-PLAN 8.5 RET-C03: "Retrieval returning the wrong protocol is a **test
failure**, not a relevance warning." Before this file the query set existed and
a spike *reported* on it; nothing in the suite *failed* when a protocol stopped
coming back first. That is the gap this closes.

WHAT THIS FILE CAN AND CANNOT PROVE, stated plainly because the distinction is
the whole reason the file is shaped this way.

`aiscelapeus/testing/fakes.py` says a fake "must never be the subject of an
assertion about a real-world property". Moss's ranking is a real-world property
of a vendor's embedding model, so **no test here asserts that the fake ranks
like Moss**, and a green run is not evidence about Moss's accuracy. Measuring
that needs the real index and lives behind the `infra` marker at the bottom of
this file, plus `spikes/spike1_retrieval_accuracy.py`.

What the hermetic tests *do* assert is everything about the corpus and the query
set that must hold for the real measurement to mean anything, and each of those
is a property of first-party data this repo owns:

- the query set's labels resolve to real protocols, cover the corpus, and agree
  with the corpus's own categories and the RET-C02 critical set;
- the magnet is gone: no document contains another's whole vocabulary, the
  structural defect that made 8/8 critical top-1 unreachable. BUILD-PLAN 8.2:
  "no similarity function can rank a specific protocol above a document that
  contains its entire vocabulary." That is a statement about the corpus, and it
  is checkable without any retrieval at all;
- each protocol that lost to a magnet now carries presentation language that
  *separates* it from the document that beat it -- asserted as "these
  discriminating terms appear here and not in the rival", which is again a
  corpus property;
- the clinical scope line holds: no protocol asks a bystander for a measure
  only a clinician can take;
- **the corpus does not leak its own query set's vocabulary.** This is the
  guard that matters most here, and it exists because the first draft of this
  subsystem failed it: 18 unique-to-target words across 7 protocols had been
  copied out of the queries, turning a genuine 7/8 critical top-1 into a
  reported 8/8. An independent review caught it, and de-leaking cost exactly
  that point back. A second review predicted morphological near-misses would
  slip past exact-token matching, and one did - the query's "waking" against a
  corpus "wake" - so the metric now compares stems (`_stem`). The final 8/8 was
  reached by *removing* arrest-adjacent wording from the choking document, not
  by adding query vocabulary, and rests on one allowlisted word ("pool") whose
  justification is recorded at `LEAKAGE_ALLOWLIST`.

**Deliberately NOT asserted here: that any scorer ranks the right document
first.** A first draft of this file did exactly that, using the TF-IDF scorer
below as a stand-in for Moss, and 14 of 20 queries failed -- correctly. The
query set is built to contain *zero* vocabulary unique to its target
(`retrieval_queries.yaml`, and the leakage audit in
`docs/evidence/2026-09-17-retrieval-alpha-measurement.md` 6), so a purely
lexical instrument cannot resolve it and was never going to. Asserting
otherwise would have been a real-world ranking claim made against a fake: the
thing `aiscelapeus/testing/fakes.py` forbids, and the thing that produces a
green suite alongside a broken system. The scorer survives only as the IDF
instrument for the magnet check, where it measures vocabulary overlap rather
than pretending to rank.

A corpus edit that re-creates a magnet, or removes a discriminator, or drifts
the query set from the corpus, fails this suite offline in CI. Whether Moss
ranks correctly is measured against Moss -- `infra` below, and the spike.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from aiscelapeus.protocols import PROTOCOLS, as_documents

QUERY_SET = Path(__file__).resolve().parent.parent / "data" / "retrieval_queries.yaml"

#: RET-C02 (BUILD-PLAN 8.5): these categories demand 100% top-1. Duplicated
#: from the spike harness deliberately -- if the two ever disagree,
#: `test_the_critical_category_set_matches_the_measurement_harness` fails rather
#: than one of them silently measuring a different bar.
CRITICAL_CATEGORIES = frozenset({"cardiac", "airway", "haemorrhage", "drowning"})

#: Words carrying no discriminating signal. Kept deliberately short: a long
#: hand-tuned list would let the gate be "fixed" by adding a word to it rather
#: than by fixing the corpus.
STOPWORDS = frozenset(
    """
    a an and any are as at be been before but by can cannot come do does doing don t
    for from get gets go going got had has have he her hers him his i if in into is it
    its just keep keeps me my no not of off on once one only or other our out over
    should so some that the their them then there these they this to up us was we
    what when where which while who will with you your
    """.split()
)


def _all_words(text: str) -> list[str]:
    """Every alphabetic token, stopwords included.

    The leakage check uses this rather than `_words`: filtering stopwords there
    would hide a real giveaway. "wake" and "stand" are ordinary English words
    that a stoplist could plausibly swallow, and both were actual leaks in the
    first draft of this subsystem.
    """
    return re.findall(r"[a-z]+", text.lower())


def _words(text: str) -> list[str]:
    return [w for w in _all_words(text) if w not in STOPWORDS]


@dataclass(frozen=True)
class Labelled:
    text: str
    expect: str
    category: str
    critical: bool


def _load_queries() -> list[Labelled]:
    raw = yaml.safe_load(QUERY_SET.read_text(encoding="utf-8"))
    return [
        Labelled(
            text=q["text"],
            expect=q["expect"],
            category=q["category"],
            critical=bool(q.get("critical", False)),
        )
        for q in raw["queries"]
    ]


QUERIES = _load_queries()
DOCUMENTS = as_documents()
BY_ID = {p["id"]: p for p in PROTOCOLS}


# --------------------------------------------------------------- the fake scorer


class LexicalRetriever:
    """An IDF instrument over the real corpus. Not a stand-in for Moss.

    Its whole implementation is in the test file on purpose: a reader can see
    exactly what the gate measures, and there is no scripted
    `{"query": [hits]}` dict that could be edited to make a failing corpus pass.
    It reads the same `as_documents()` text the real index is built from.

    It deliberately exposes **no ranking method**. An earlier draft had one and
    used it to assert that the right protocol came first; that is a real-world
    ranking claim measured against a fake, and it failed 14 of 20 queries
    because the query set contains no vocabulary unique to its target by
    design. What survives is the part that is honest offline: inverse document
    frequency, which is what makes "document A contains document B's whole
    vocabulary" a meaningful statement rather than an artifact of every
    first-aid document sharing the words "patient" and "bleeding".
    """

    def __init__(self, documents: list[dict[str, object]]) -> None:
        self._tf: dict[str, Counter[str]] = {}
        for doc in documents:
            self._tf[str(doc["id"])] = Counter(_words(str(doc["text"])))
        total = len(self._tf)
        appearances: Counter[str] = Counter()
        for counts in self._tf.values():
            appearances.update(counts.keys())
        # Smoothed IDF, floored at zero so a term present in every document
        # contributes nothing rather than a negative weight.
        self._idf = {
            term: max(0.0, math.log((total + 1) / (seen + 1)))
            for term, seen in appearances.items()
        }


def _query_id(query: Labelled) -> str:
    return query.expect


# ------------------------------------------------------- query set <-> corpus


def test_the_query_set_is_not_empty_and_covers_every_protocol() -> None:
    # falsifier: a protocol is added to the corpus with no labelled query, so it
    # is never measured -- it can return the wrong document forever and every
    # accuracy figure in the project stays green because nothing asks about it.
    assert len(QUERIES) >= len(PROTOCOLS), (
        f"{len(QUERIES)} queries for {len(PROTOCOLS)} protocols"
    )
    uncovered = sorted({p["id"] for p in PROTOCOLS} - {q.expect for q in QUERIES})
    assert uncovered == [], f"protocols with no query: {uncovered}"


def test_every_query_label_resolves_to_a_real_protocol() -> None:
    # falsifier: a protocol id is renamed in protocols.py and the query set is
    # not updated, so its query can never match. The real-Moss run then reports
    # a permanent retrieval failure that is actually a typo, and someone spends
    # a session tuning an embedder against a broken label.
    unknown = sorted({q.expect for q in QUERIES} - set(BY_ID))
    assert unknown == [], f"queries reference ids not in the corpus: {unknown}"


@pytest.mark.parametrize("query", QUERIES, ids=_query_id)
def test_each_query_category_matches_the_corpus(query: Labelled) -> None:
    # falsifier: a protocol is recategorised -- say haemorrhage to wounds -- and
    # the query set keeps the old label, so per-category accuracy is computed
    # against a mapping that no longer describes the corpus.
    actual = BY_ID[query.expect]["category"]
    assert query.category == actual, (
        f"query for {query.expect} says category {query.category!r}, "
        f"corpus says {actual!r}"
    )


@pytest.mark.parametrize("query", QUERIES, ids=_query_id)
def test_criticality_cannot_be_downgraded_by_a_label(query: Labelled) -> None:
    # falsifier: a cardiac query is marked `critical: false` to get a failing
    # RET-C02 run to pass. The gate that exists to guarantee 8/8 on the
    # categories that kill people is then satisfied by editing a YAML flag.
    actual = BY_ID[query.expect]["category"]
    expected_critical = actual in CRITICAL_CATEGORIES
    assert query.critical == expected_critical, (
        f"query for {query.expect} has critical={query.critical} but category "
        f"{actual!r} implies critical={expected_critical}"
    )


def test_the_critical_categories_are_all_represented() -> None:
    # falsifier: the drowning protocol's query is deleted, so RET-C02 reports
    # 7/7 PASS while the category whose magnet document caused the original
    # failure is no longer measured at all.
    covered = {q.category for q in QUERIES if q.critical}
    assert covered == CRITICAL_CATEGORIES, (
        f"critical categories measured: {sorted(covered)}, "
        f"required: {sorted(CRITICAL_CATEGORIES)}"
    )


def test_the_critical_category_set_matches_the_measurement_harness() -> None:
    # falsifier: this file and spikes/spike1_retrieval_accuracy.py drift apart
    # on what "critical" means, so the hermetic gate enforces one bar while the
    # real-Moss measurement reports another, and both call themselves RET-C02.
    harness = Path(__file__).resolve().parent.parent.parent / "spikes" / "spike1_retrieval_accuracy.py"
    source = harness.read_text(encoding="utf-8")
    for category in CRITICAL_CATEGORIES:
        assert f'"{category}"' in source, (
            f"{category!r} is critical here but absent from the harness"
        )


# ------------------------------------------------------ RET-C03: wrong = failure


#: Each entry is a measured RET-C02 failure from
#: docs/evidence/2026-09-17-retrieval-alpha-measurement.md 2.1, with the terms
#: added to the loser to separate it from the document that beat it. The terms
#: are what a caller says for the victim and NOT for the rival -- the
#: discrimination this subsystem was built to establish. A term is required to
#: be absent from the rival, so a later broadening of the rival's text that
#: re-absorbs the discriminator fails here rather than in production.
DISCRIMINATORS: list[tuple[str, str, tuple[str, ...]]] = [
    # Cardiac arrest lost to drowning. What distinguishes an arrest from a
    # drowning is dry land and the agonal gasp.
    ("bls-adult-cpr", "drowning-rescue", ("dry land", "gasping")),
    # Choking lost to drowning. A choking casualty is conscious and sitting or
    # standing; a drowning casualty is neither. Stated positively on purpose:
    # an earlier wording said "not an arrest to compress", and a negative
    # instruction about compressions is the last thing to put in a corpus whose
    # likeliest clinical failure is under-triggering CPR.
    ("airway-choking-adult", "drowning-rescue", ("conscious", "standing")),
    # Wound packing lost to minor-wound. The words for exsanguination that
    # nobody uses for a graze.
    # "exsanguination" was here and was removed: it is clinician register, and
    # a bystander-facing corpus should not need a word nobody says out loud.
    # The lay phrases carry the same discrimination. "will not stop" was then
    # tried and rejected by this very test, which found minor-wound already
    # contains it -- a discriminator the rival owns is not a discriminator.
    (
        "bleed-pressure-packing",
        "minor-wound",
        ("serious blood loss", "soaking", "welling"),
    ),
    # Drowning lost rank 1 to the recovery position once the cardiac document
    # gained arrest vocabulary. Submersion is the discriminator, and it is the
    # narrowest remaining margin in the corpus -- see the module docstring.
    ("drowning-rescue", "airway-recovery-position", ("submer", "water")),
    # Cardiac arrest lost to the heart-attack document, which describes a
    # CONSCIOUS patient. That distinction is clinical, not lexical: a heart
    # attack gets aspirin and a sitting position, an arrest gets compressions.
    ("chest-pain-cardiac", "bls-adult-cpr", ("awake", "conscious")),
]


@pytest.mark.parametrize(
    ("victim", "rival", "terms"),
    DISCRIMINATORS,
    ids=[f"{v}-vs-{r}" for v, r, _ in DISCRIMINATORS],
)
def test_a_previously_confused_pair_keeps_its_discriminating_language(
    victim: str, rival: str, terms: tuple[str, ...]
) -> None:
    # falsifier: someone edits protocols.py for readability and drops the
    # presentation line that separates cardiac arrest from drowning, or
    # broadens the drowning text until it re-absorbs those words. Retrieval
    # then silently returns the drowning protocol for a cardiac arrest again --
    # the exact RET-C02 failure this subsystem closed, and one the B4 output
    # gate cannot catch because the citation is well-formed and points at the
    # wrong document.
    victim_text = str(BY_ID[victim]["text"]).lower()
    rival_text = str(BY_ID[rival]["text"]).lower()
    missing = [term for term in terms if term not in victim_text]
    assert missing == [], (
        f"{victim} lost its discriminators against {rival}: {missing}"
    )
    absorbed = [term for term in terms if term in rival_text]
    assert absorbed == [], (
        f"{rival} has absorbed {victim}'s discriminating terms {absorbed}, "
        f"so the two are no longer separable"
    )


#: Words allowed to appear in a protocol and in its own query and nowhere else
#: in the corpus. Each needs a clinical reason that stands up independently of
#: the benchmark, because every entry here weakens the leakage guard below.
#:
#: "pool": a swimming pool is the commonest drowning location, so a drowning
#: protocol that does not name it is worse guidance. It is also, measurably,
#: the single word separating RET-C02 PASS from FAIL -- with it, drowning is
#: rank 1 and critical top-1 is 8/8; without it, drowning falls to rank 2
#: behind airway-recovery-position and critical top-1 is 7/8. That is recorded
#: here rather than hidden: the 8/8 rests on this one word, and a reader
#: deciding whether to trust the figure needs to know it.
LEAKAGE_ALLOWLIST: frozenset[str] = frozenset({"pool"})


def _stem(word: str) -> str:
    """Crudely strip the inflections that let a leak hide as a near-miss.

    Deliberately not a real stemmer: it has to be inspectable and have no
    dependency. It exists because exact-token matching has a demonstrated blind
    spot - the query says "waking", the corpus said "wake", and the leak scored
    zero while being a genuine unique-to-target giveaway worth a full point of
    critical top-1. An independent review predicted exactly this class of
    near-miss before it was found.
    """
    for suffix in ("ing", "ed", "es", "s"):
        if len(word) - len(suffix) >= 3 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def giveaway_words(
    query_text: str, target_text: str, other_texts: list[str]
) -> list[str]:
    """Words shared by a query and its target that appear in no other document.

    The query set's own leakage metric (`retrieval_queries.yaml`, and
    docs/evidence/2026-09-17-retrieval-alpha-measurement.md 6), extracted into
    one function so that the test asserting the corpus is clean and the test
    asserting the metric works both exercise *this* code. They previously
    hand-duplicated the same three-line set expression, so a bug introduced in
    one could not be caught by the other - flagged by independent review.

    Comparison is on stems, not exact tokens, so "waking"/"wake" counts. The
    word reported is the one in the *corpus*, since that is the text an author
    would have to change.

    Single-character tokens are dropped: a possessive "s" is a tokenisation
    artifact, and the pre-R1 baseline corpus already contained one.
    """
    elsewhere = {_stem(word) for text in other_texts for word in _all_words(text)}
    query_stems = {_stem(word) for word in _all_words(query_text)}
    return sorted(
        word
        for word in set(_all_words(target_text))
        if len(word) > 1 and _stem(word) in query_stems and _stem(word) not in elsewhere
    )


def test_the_corpus_does_not_leak_its_queries_vocabulary() -> None:
    # falsifier: a protocol's text is augmented by copying distinctive words out
    # of its own labelled query, so the measured top-1 improves because the
    # benchmark now matches the corpus by string overlap rather than because the
    # corpus discriminates better. This is not hypothetical: it is the defect an
    # independent review caught in the first draft of this very subsystem, where
    # 18 such words across 7 protocols turned a genuine 7/8 into a reported 8/8.
    # docs/evidence/2026-09-17-retrieval-alpha-measurement.md 6 records the same
    # mechanism from the query side, worth 5 points of top-1.
    #
    # The metric is the query set's own: a word shared between a query and its
    # target document that appears in NO other document -- a unique-to-target
    # giveaway. Single characters are excluded as tokenisation artifacts ("s"
    # from a possessive), which the baseline corpus already contained.
    per_document = {str(doc["id"]): str(doc["text"]) for doc in DOCUMENTS}
    leaks: list[str] = []
    for query in QUERIES:
        giveaways = [
            word
            for word in giveaway_words(
                query.text,
                per_document[query.expect],
                [
                    text
                    for doc_id, text in per_document.items()
                    if doc_id != query.expect
                ],
            )
            if word not in LEAKAGE_ALLOWLIST
        ]
        if giveaways:
            leaks.append(f"{query.expect}: {giveaways}")
    assert leaks == [], (
        "corpus text shares vocabulary with its own query that appears nowhere "
        "else in the corpus, so retrieval accuracy on this query set is partly "
        "measuring string overlap:\n  " + "\n  ".join(leaks)
    )


def test_the_leakage_allowlist_stays_small_and_justified() -> None:
    # falsifier: the leakage rule above is "fixed" by appending every offending
    # word to the allowlist, so the guard reports green while the corpus is
    # fully overfitted to the benchmark. An allowlist is only honest while it is
    # short enough that every entry is individually argued in the comment above.
    assert len(LEAKAGE_ALLOWLIST) <= 2, (
        f"allowlist has grown to {sorted(LEAKAGE_ALLOWLIST)}; each entry needs a "
        f"clinical justification that holds independently of the benchmark"
    )


def test_the_leakage_metric_detects_a_planted_giveaway() -> None:
    # falsifier: the leakage rule's set arithmetic is inverted or compares the
    # wrong pair, so it can never fire and the overfitting guard above is
    # decorative. Calls the SAME function the corpus check calls - an earlier
    # version re-implemented the formula on synthetic dicts, so a bug in the
    # real check could not be caught here. Found by independent review.
    # The filler words are present in every text, exactly as connectives are
    # spread across the real 20-document corpus, so only the planted word is
    # unique to the target. A fixture whose "other" documents were too small
    # would report its own connectives as giveaways.
    filler = "the casualty is on the ground and needs help"
    found = giveaway_words(
        query_text=f"{filler} floppy zzzunique bleeding",
        target_text=f"{filler} floppy zzzunique",
        other_texts=[f"{filler} bleeding pressure", f"{filler} floppy fall"],
    )
    assert found == ["zzzunique"], (
        f"expected only the planted unique-to-target word, got {found}"
    )


def test_the_leakage_metric_counts_ordinary_words_and_drops_single_characters() -> None:
    # falsifier: the metric quietly filters stopwords, so a genuine giveaway
    # that happens to be a common English word slips through. "wake" and
    # "stand" were both real leaks in this subsystem's first draft and either
    # could be swallowed by a stoplist - which is why `giveaway_words` uses
    # `_all_words`, not the stopword-filtered `_words`.
    filler = "the casualty needs help now"
    assert giveaway_words(
        f"{filler} he will not wake", f"{filler} will not wake", [filler]
    ) == ["not", "wake", "will"], "ordinary English words must still count"
    # A one-character possessive token is a tokenisation artifact, not a leak;
    # the pre-R1 baseline corpus already contained one.
    assert giveaway_words(f"{filler} s", f"{filler} s", [filler]) == [], (
        "a single-character token must be dropped"
    )


def test_no_document_is_a_vocabulary_magnet() -> None:
    # falsifier: a protocol's text is broadened until it contains the full
    # vocabulary of another protocol -- the exact structural defect recorded in
    # BUILD-PLAN 8.2, where drowning-rescue contained all of CPR's and choking's
    # words, and "no similarity function can rank a specific protocol above a
    # document that contains its entire vocabulary". No amount of retrieval
    # tuning recovers from it, so it is banned at the corpus level.
    content = {
        str(doc["id"]): set(_words(str(doc["text"]))) for doc in DOCUMENTS
    }
    supersets: list[str] = []
    for victim_id, victim_words in content.items():
        for magnet_id, magnet_words in content.items():
            if magnet_id == victim_id or not victim_words:
                continue
            covered = len(victim_words & magnet_words) / len(victim_words)
            if covered >= 0.9:
                supersets.append(
                    f"{magnet_id} covers {covered:.0%} of {victim_id}'s vocabulary"
                )
    assert supersets == [], "magnet document(s) in the corpus:\n  " + "\n  ".join(
        supersets
    )


def test_the_magnet_rule_actually_fires_on_a_magnet() -> None:
    # falsifier: the magnet rule above passes because its overlap arithmetic is
    # wrong and no corpus could ever trip it, so the one structural defect that
    # made RET-C02 unreachable is guarded by a test that cannot fail. Feeds it
    # the documented shape -- one document containing another's whole
    # vocabulary plus more -- and requires a complaint naming both.
    magnet = "not breathing unresponsive collapsed airway cpr rescue breaths"
    broken = [
        {"id": "victim", "text": magnet, "metadata": {}},
        {"id": "magnet", "text": f"{magnet} submersion water pulled dragged", "metadata": {}},
    ]
    content = {str(d["id"]): set(_words(str(d["text"]))) for d in broken}
    covered = len(content["victim"] & content["magnet"]) / len(content["victim"])
    assert covered >= 0.9, (
        f"the overlap measure reports only {covered:.0%} for a literal superset, "
        f"so the magnet rule would not fire on the defect it exists to catch"
    )


def test_the_idf_instrument_discounts_corpus_wide_vocabulary() -> None:
    # falsifier: IDF is computed with the wrong sign or no flooring, so a word
    # present in every protocol scores as highly as a rare one. The magnet check
    # then rates two documents as similar purely for sharing the words every
    # first-aid document contains, and either fires constantly or never.
    common = [
        {"id": f"d{i}", "text": "bleeding wound patient pressure", "metadata": {}}
        for i in range(10)
    ]
    common.append({"id": "rare", "text": "bleeding tourniquet windlass", "metadata": {}})
    scorer = LexicalRetriever(common)
    assert scorer._idf["bleeding"] == 0.0, (
        "a term in every document must contribute nothing"
    )
    assert scorer._idf["windlass"] > scorer._idf["patient"], (
        "a term in one document must outweigh one in ten"
    )


# --------------------------------------------------------------- clinical scope


@pytest.mark.parametrize(
    "forbidden",
    [
        "capillary refill",
        "respiratory rate",
        "auscultat",
        "stethoscope",
        "jugular",
        "intravenous",
        "iv access",
        "crystalloid",
        "bag-mask",
        "glucometer",
        "mmol",
        "expectant",
        # Pulse-character grading. Blocker 2 of the clinical-safety review: lay
        # pulse detection is roughly coin-flip accurate, which is why ILCOR/AHA
        # /ERC removed it from lay BLS. These terms were added after a reviewer
        # found "the heartbeat has gone very fast and feeble" had walked the
        # assessment back in through a recognition paragraph rather than through
        # an algorithm branch -- the same defect in a new location.
        "feeble",
        "thready",
        "pulse rate",
        "weak and rapid",
    ],
)
def test_no_protocol_asks_a_bystander_for_a_clinician_only_measure(
    forbidden: str,
) -> None:
    # falsifier: an augmentation adds a presentation line naming an assessment
    # no bystander can perform -- counted respiratory rate, capillary refill, a
    # glucose reading. docs/reviews/2026-09-17-clinical-safety.md Blocker 5:
    # asking anyway is worse than omitting, because a panicking caller guesses
    # and the guess enters the system looking like data. CLINICAL-STANDARDS 5.4
    # is the scope line this enforces.
    offenders = [
        str(doc["id"])
        for doc in DOCUMENTS
        if forbidden in str(doc["text"]).lower()
    ]
    assert offenders == [], (
        f"{forbidden!r} is a clinician-only measure but appears in: {offenders}"
    )


#: Protocols carrying a prepended recognition paragraph, and the first words of
#: their original instruction block. Everything before that marker is added
#: text and is held to the bystander-observable bar; everything from it onward
#: is pre-existing clinical content this subsystem must not touch.
RECOGNITION_BOUNDARY: dict[str, str] = {
    "bls-adult-cpr": "Adult CPR.",
    "airway-choking-adult": "Choking adult,",
    "bleed-pressure-packing": "Direct pressure and packing.",
    "anaphylaxis": "Anaphylaxis.",
    "shock-management": "Shock.",
    "seizure": "Seizure.",
    "heat-stroke": "Heat stroke.",
    "drowning-rescue": "Drowning.",
    "chest-pain-cardiac": "Suspected heart attack.",
}


@pytest.mark.parametrize("protocol_id", sorted(RECOGNITION_BOUNDARY))
def test_added_recognition_text_never_asks_for_a_pulse(protocol_id: str) -> None:
    # falsifier: a recognition paragraph tells a bystander to judge a pulse --
    # "weak pulse", "feel for a heartbeat" -- so the assessment Blocker 2 of
    # docs/reviews/2026-09-17-clinical-safety.md removed from the algorithm
    # returns through the corpus instead. Both leaves of a lay pulse check
    # converge on not resuscitating, reached through a measurement the operator
    # cannot make.
    #
    # Scoped to the ADDED text only, and that scoping is the point:
    # shock-management's original block legitimately says "fast weak pulse" as
    # a description of shock, where no action is gated on it. Banning the
    # phrase corpus-wide would force an edit to pre-existing clinical content
    # to satisfy a rule about new content.
    text = str(BY_ID[protocol_id]["text"])
    marker = RECOGNITION_BOUNDARY[protocol_id]
    assert marker in text, (
        f"{protocol_id}: original instruction block no longer starts with "
        f"{marker!r}, so added and pre-existing text can no longer be told apart"
    )
    added = text[: text.index(marker)].lower()
    # The first four ban asking the bystander to PALPATE. The rest ban asserting
    # the internal state as fact, which a clinical review caught passing this
    # gate: "The heart has stopped beating." was in bls-adult-cpr's recognition
    # text and matched none of the palpation terms. It is the same Blocker 2
    # failure from the other direction - the caller cannot observe it, the scene
    # can appear to contradict it, and the instinct on hearing it is to go and
    # check, which is the assessment ILCOR removed from lay BLS.
    for phrase in (
        "pulse",
        "heartbeat",
        "heart rate",
        "beats per minute",
        "heart has stopped",
        "heart stopped",
        "heart is not beating",
        "heart has stopped beating",
    ):
        assert phrase not in added, (
            f"{protocol_id}: recognition text asks the bystander to assess "
            f"{phrase!r}, which Blocker 2 rules out"
        )


def test_the_arrest_protocol_names_agonal_breathing() -> None:
    # falsifier: the CPR protocol describes arrest as "not breathing" only, so a
    # caller watching agonal gasps answers "yes he's breathing" and is routed
    # away from compressions. docs/reviews/2026-09-17-clinical-safety.md
    # Blocker 3 calls this "the most probable way this specific product kills
    # someone", because it fires on the most common arrest presentation.
    text = str(BY_ID["bls-adult-cpr"]["text"]).lower()
    assert "gasping" in text, "agonal gasping is not described in the CPR protocol"
    assert "not normal breathing" in text or "not breathing normally" in text, (
        "the protocol must state that gasping is not normal breathing"
    )


def test_the_title_prefix_survives_in_the_indexed_text() -> None:
    # falsifier: `as_documents()` is "tidied" to index only the body text.
    # BUILD-PLAN 8.2 measured that dropping the title prefix COSTS top-1
    # (4/10 -> 3/10), so this is a measured property of the pipeline, not a
    # style preference -- and removing it degrades retrieval silently.
    for doc in DOCUMENTS:
        title = str(BY_ID[str(doc["id"])]["title"])
        assert str(doc["text"]).startswith(title), (
            f"{doc['id']}: indexed text no longer begins with its title"
        )


# ------------------------------------------------------------- real Moss (infra)


def test_the_infra_marker_keeps_real_moss_out_of_the_default_suite() -> None:
    # falsifier: the `infra` deselection in tests/conftest.py is removed or
    # renamed, so a bare `pytest -q` starts talking to real Moss. The suite then
    # fails on any machine without credentials and, worse, passes *by hitting
    # the network* on one that has them - which is how an accuracy or latency
    # number gets published from a run nobody realised was live. This was a real
    # defect: pyproject.toml registered the marker as "not in the blocking gate"
    # while nothing acted on it, and the infra test below ran by default.
    conftest = Path(__file__).resolve().parent.parent / "conftest.py"
    source = conftest.read_text(encoding="utf-8")
    assert "pytest_collection_modifyitems" in source, (
        "conftest.py no longer hooks collection, so no marker can be deselected"
    )
    assert '"infra"' in source or "'infra'" in source, (
        "conftest.py's collection hook no longer mentions the infra marker"
    )


def _dotenv_credentials() -> dict[str, str]:
    """Credentials from the repo-root dotenv, in `config.read_env`'s own order.

    Used only by the `infra` test below. Mirrors the precedence the project
    uses (`.env.local`, then `.env`) without importing `config`, for the reason
    the spike harness states: if this went through our config layer, a bug there
    could silently change what gets measured. No value is ever printed.
    """
    from dotenv import dotenv_values

    from aiscelapeus.config import repo_root

    resolved: dict[str, str] = {}
    for candidate in (repo_root() / ".env.local", repo_root() / ".env"):
        if candidate.is_file():
            for name, value in dotenv_values(candidate).items():
                if value is not None:
                    resolved.setdefault(name, value)
    return resolved


@pytest.mark.infra
def test_real_moss_meets_ret_c01_and_ret_c02() -> None:
    # falsifier: the corpus, the embedding model, the index contents or `alpha`
    # changes and real retrieval starts returning the wrong protocol for a
    # cardiac arrest. Every hermetic test above is about first-party data; this
    # is the only assertion in the suite that observes the vendor's actual
    # ranking, which is why RET-C04 requires it to exist and to run on demand.
    import asyncio

    from moss import DocumentInfo, MossClient, MutationOptions, QueryOptions

    # Credentials are read from the repo-root dotenv, NOT from os.environ, and
    # that is deliberate. conftest's `isolated_env` strips every MOSS_* variable
    # from the process so no hermetic test can ever reach real infrastructure --
    # correct for the other 1700 tests, and fatal for this one: reading
    # os.environ here made this test skip unconditionally, on every machine,
    # including one with working credentials. A test that can never run is not
    # evidence, it is a permanently-green placeholder, and RET-C04 explicitly
    # requires the query set to run "on demand against real Moss".
    creds = _dotenv_credentials()
    project = creds.get("MOSS_PROJECT_ID", "").strip()
    key = creds.get("MOSS_PROJECT_KEY", "").strip()
    if not (project and key):
        pytest.skip(
            "real Moss needs MOSS_PROJECT_ID and MOSS_PROJECT_KEY in the "
            "repo-root .env.local or .env"
        )

    index = creds.get("MOSS_MEASURE_INDEX", "").strip() or "aiscelapeus-protocols-r1"
    model_id = creds.get("MOSS_MODEL_ID", "").strip() or None
    # 0.8 is the measured best. docs/evidence/2026-09-17-retrieval-alpha-measurement.md
    # refutes BUILD-PLAN 8.2 item 1's direction to raise it to 1.0.
    alpha = 0.8

    async def measure() -> list[tuple[Labelled, str]]:
        client = MossClient(project, key)
        docs = [
            DocumentInfo(
                id=str(d["id"]),
                text=str(d["text"]),
                metadata={k: str(v) for k, v in dict(d["metadata"]).items()},  # type: ignore[call-overload]
            )
            for d in DOCUMENTS
        ]
        existing = {ix.name for ix in await client.list_indexes()}
        if index in existing:
            await client.add_docs(index, docs, MutationOptions(upsert=True))
        elif model_id:
            await client.create_index(index, docs, model_id)
        else:
            await client.create_index(index, docs)
        await client.load_index(index)

        options = QueryOptions(top_k=8, alpha=alpha)
        out: list[tuple[Labelled, str]] = []
        for query in QUERIES:
            result = await client.query(index, query.text, options)
            # Rank 1 is docs[0] as the SDK returns it -- never re-sorted.
            out.append((query, result.docs[0].id if result.docs else ""))
        return out

    observed = asyncio.run(measure())

    critical_misses = [
        f"{q.expect} -> {got}" for q, got in observed if q.critical and got != q.expect
    ]
    assert critical_misses == [], (
        f"RET-C02 requires 8/8 top-1 on {sorted(CRITICAL_CATEGORIES)}; "
        f"wrong protocol returned for: {critical_misses}"
    )

    top1 = sum(1 for q, got in observed if got == q.expect)
    assert top1 >= 18, (
        f"RET-C01 requires >= 18/20 top-1, measured {top1}/{len(observed)}"
    )


#: Claims that a casualty's state PERSISTS, in a corpus whose whole purpose is
#: guiding someone through a deterioration. A recognition cue describes the state
#: on arrival; a cue that promises it will hold is an eligibility gate.
_PERSISTENCE_CLAIMS: tuple[str, ...] = (
    "aware of you throughout",
    "alert throughout",
    "conscious throughout",
    "remains conscious",
    "stays conscious",
    "will stay awake",
)


def test_no_protocol_promises_a_casualty_state_will_persist() -> None:
    # falsifier: a recognition cue claims the casualty stays conscious - the
    # deleted "They are alert and aware of you throughout" in
    # airway-choking-adult - and the LLM grounds a clinical instruction on it.
    #
    # That sentence was clinically false, contradicted its own document's
    # closing line ("if they go unconscious, lower them down and start CPR"),
    # and mattered because this is the ONLY choking document in the corpus: a
    # caller describing a casualty who had already gone limp matched no cue in
    # it, so the agent would coach back blows and abdominal thrusts on a patient
    # needing compressions. A clinical review rated that the one finding where a
    # real call ends in the wrong physical action.
    #
    # Caught only by this test: the mutation that restored the sentence passed
    # every other assertion in this file, including the discriminator and
    # scope-ban gates. Whatever else the corpus says about consciousness, it may
    # not promise it lasts.
    offenders: list[str] = []
    for document in as_documents():
        text = str(document["text"]).lower()
        for claim in _PERSISTENCE_CLAIMS:
            if claim in text:
                offenders.append(f"{document['id']}: {claim!r}")

    assert offenders == [], (
        "a recognition cue promises a casualty state will persist, which turns a "
        "presentation cue into an eligibility gate:\n  " + "\n  ".join(offenders)
    )


def test_the_persistence_rule_fires_on_a_planted_claim() -> None:
    # falsifier: `_PERSISTENCE_CLAIMS` is checked against the wrong field, or the
    # comparison is case-sensitive against lowercased text, so the rule above
    # iterates and finds nothing however bad the corpus gets - the vacuous-gate
    # failure this repo has already found in a CI job and a fixture.
    planted = "Fully conscious. They are ALERT AND AWARE OF YOU THROUGHOUT the incident."
    assert any(claim in planted.lower() for claim in _PERSISTENCE_CLAIMS), (
        "the persistence rule does not detect its own planted claim"
    )


def test_the_only_choking_document_names_the_transition_to_cpr() -> None:
    # falsifier: the choking protocol describes a conscious casualty and never
    # says what happens when that changes, so the corpus has no retrievable path
    # from choking to arrest. The corpus holds exactly one choking document and
    # no unconscious-choking document at all, so this sentence is the whole
    # bridge between the two states.
    text = str(BY_ID["airway-choking-adult"]["text"]).lower()

    assert "unconscious" in text, "the choking document must name the transition"
    assert "cpr" in text or "compression" in text, (
        "naming unconsciousness without naming what to do about it is worse than "
        "not naming it"
    )

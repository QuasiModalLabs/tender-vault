"""
The Jev family imputer's structural guarantees (ref-004).

Plain Python, no pytest - `python tests/test_family_imputer.py`, exit 0 means
passed. Same shape as test_filter_audit.py.

These test the BOUNDARIES, not the model: that the imputer cannot see the
codes, cannot read the GSIN side of the PSPC file, cannot run on a drifted
question or an unpinned model, that its comparator is production's own
function, that nothing in the product imports it, and that the key never
leaves the Authorization header. No test here makes a network call.
"""
from __future__ import annotations

import ast
import json
import re
import sqlite3
import sys
import tempfile
from collections import Counter
from dataclasses import fields
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

import conftest  # noqa: E402,F401  — MUST come first: redirects the vault

import ingest  # noqa: E402
from family_imputer import cache, client, comparator, evaluate  # noqa: E402
from family_imputer import options as O  # noqa: E402
from family_imputer import question as Q  # noqa: E402
from family_imputer.state import ImputerState, prepare  # noqa: E402
from filter_audit import blinding  # noqa: E402
from filter_audit import predicates as P  # noqa: E402

FAILURES: list[str] = []
SCRIPTS = Path(__file__).parent.parent / "scripts"


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  PASS  {label}")
    else:
        FAILURES.append(f"{label}: expected {want!r}, got {got!r}")
        print(f"  FAIL  {label}: expected {want!r}, got {got!r}")


def raises(label: str, exc_type, fn) -> None:
    try:
        fn()
    except exc_type:
        print(f"  PASS  {label}")
        return
    except Exception as other:  # noqa: BLE001
        FAILURES.append(f"{label}: raised {type(other).__name__}, wanted {exc_type.__name__}")
        print(f"  FAIL  {label}: raised {type(other).__name__}: {other}")
        return
    FAILURES.append(f"{label}: did not raise")
    print(f"  FAIL  {label}: did not raise")


# ---------------------------------------------------------------------------

def test_state_is_blind() -> None:
    print("\nImputerState carries title and description, and nothing else")
    check("ImputerState fields are exactly {title, description}",
          {f.name for f in fields(ImputerState)}, {"title", "description"})

    class OnlyTwoKeys(dict):
        def __getitem__(self, key):
            if key not in ("title", "description"):
                raise AssertionError(f"from_row read {key!r}")
            return dict.__getitem__(self, key)

    row = OnlyTwoKeys(title="T", description="D", unspsc="*81111500",
                      gsin="D302A", contracting_entity="SSC")
    raises_nothing = True
    try:
        state = ImputerState.from_row(row)
    except AssertionError:
        raises_nothing = False
        state = None
    check("from_row reads no key but title and description", raises_nothing, True)
    check("...and the payload sent holds only those two", sorted(prepare(state).payload),
          ["description", "title"])


def test_truncation_is_recorded() -> None:
    print("\nTruncation is flagged and changes the content hash")
    long = ImputerState("t", "x" * 100)
    a, b = prepare(long, budget=1000), prepare(long, budget=50)
    check("an untruncated notice is not flagged", a.truncated, False)
    check("a truncated notice is flagged", b.truncated, True)
    check("...with its original length", b.original_chars, 101)
    check("a different budget is a cache miss, not a stale hit",
          a.content_sha256 != b.content_sha256, True)


def _fake_reference(tmp: Path) -> Path:
    path = tmp / "ref.csv"
    header = ("UNSPSC-Code,UNSPSC-Description-eng,UNSPSC-Description-fra,"
              "HierarchyLevel-NiveauHierarchique,CommodityType-TypeProduit-eng,"
              "CommodityType-TypeProduit-fra,GSINCode-NIBSCode,"
              "GSINDescription-NIBSDescription-eng,GSINDescription-NIBSDescription-fra,"
              "date-file-published")
    rows = [
        ("81000000", "Engineering segment", "L1"), ("80000000", "Mgmt segment", "L1"),
        ("72000000", "Construction segment", "L1"),
        ("81110000", "Computer services", "L2"), ("81100000", "Engineering", "L2"),
        ("80100000", "Management advisory services", "L2"),
        ("80101500", "Business consultation", "L3"),
        ("80101507", "Information technology consultation services", "L4"),
        ("72100000", "Building maintenance", "L2"),
    ]
    lines = [header] + [f'{c},"{d}","fr",{lvl},Services,Services,5153,'
                        f'"GSIN_SENTINEL_TEXT","fr",2021-05-12' for c, d, lvl in rows]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def test_gsin_never_loads() -> None:
    print("\nThe GSIN side of the PSPC file is never held")
    with tempfile.TemporaryDirectory() as tmp:
        ref = O.load_reference(_fake_reference(Path(tmp)))
    check("no loaded entry carries a GSIN value",
          any("GSIN_SENTINEL_TEXT" in repr(e) or "5153" in repr(e) for e in ref.values()),
          False)
    check("the whitelist is three UNSPSC columns",
          O.UNSPSC_COLUMNS, ("UNSPSC-Code", "UNSPSC-Description-eng",
                             "HierarchyLevel-NiveauHierarchique"))


def test_options_and_mapping() -> None:
    print("\nOptions are deterministic and codes map by longest prefix")
    with tempfile.TemporaryDirectory() as tmp:
        ref = O.load_reference(_fake_reference(Path(tmp)))
    opts = O.build_options(ref, ["8111", "80101507"], ("72",))
    kinds = {o.key: o.kind for o in opts}
    check("profile entries become profile options, L4 kept at L4",
          sorted(k for k, v in kinds.items() if v == "profile"),
          ["80101507 Information technology consultation services", "8111 Computer services"])
    carved = [o for o in opts if o.prefix == "8010"][0]
    check("the family holding a carved-out L4 says so in its key",
          carved.key, "8010 Management advisory services (other than 80101507)")
    check("building twice gives the same tuple",
          opts == O.build_options(ref, ["8111", "80101507"], ("72",)), True)
    m = lambda c: O.map_code(c, opts)  # noqa: E731
    check("80101507 -> profile option", m("80101507"),
          "80101507 Information technology consultation services")
    check("80101501 -> the carved sibling", m("80101501"), carved.key)
    check("81111500 -> 8111", m("81111500"), "8111 Computer services")
    check("81000000 (segment-only, profile segment) -> unmappable", m("81000000"), O.UNMAPPABLE)
    check("72101504 -> segment option", m("72101504"), "72 Construction segment")
    check("10101501 -> none", m("10101501"), O.NONE_KEY)
    raises("a segment already split into families is refused", ValueError,
           lambda: O.build_options(ref, ["8111"], ("81",)))


def test_question_is_frozen() -> None:
    print("\nThe question refuses to run when it drifts")
    if not O.REFERENCE_CSV.exists():
        print("  SKIP  live hash check: .cache/unspsc_reference.csv absent")
    else:
        live = Q.build_question()
        check("the live question matches the recorded literal",
              live.sha256, Q.QUESTION_SHA256)
        edited = dict(live.payload, instructions=live.payload["instructions"] + " ")
        drifted = Q.Question(live.options, edited, Q.question_sha256(edited))
        raises("an edited question raises FrozenQuestionDrift",
               Q.FrozenQuestionDrift, lambda: Q.check_frozen(drifted))
        check("the instructions name no company, competency, IT or fit",
              any(w in Q.INSTRUCTIONS.lower() for w in (
                  "information technology", " it services", "competenc", "our ",
                  "company", "firm", " fit", "relevant")),
              False)
    raises("any recorded hash other than the live one refuses",
           Q.FrozenQuestionDrift,
           lambda: Q.check_frozen(Q.Question((), {}, "a" * 64), recorded="b" * 64))


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class _Session:
    def __init__(self, resp):
        self.resp, self.sent = resp, []

    def post(self, url, json=None, headers=None, timeout=None):  # noqa: A002
        self.sent.append({"url": url, "json": json, "headers": headers})
        return self.resp


def _body(model="jev-1.13.0", choice="a", probs=None):
    return {"model": model,
            "answers": {Q.QUESTION_ID: {"type": "choice", "choice": choice,
                                        "probabilities": probs or {"a": 0.9, "b": 0.1},
                                        "confidence": 0.8}},
            "usage": {"input_tokens": 100, "output_tokens": 3}}


def test_model_is_pinned_and_answer_validated() -> None:
    print("\nThe pinned model is enforced and answers are validated")
    raises("a response from another model version is refused",
           client.ModelMismatch, lambda: client.validate_answer(_body(model="jev-1.14.0"), ("a", "b")))
    raises("'jev-latest' in the response is refused too",
           client.ModelMismatch, lambda: client.validate_answer(_body(model="jev-latest"), ("a", "b")))
    raises("a choice outside the options is refused",
           client.JevError, lambda: client.validate_answer(_body(choice="z"), ("a", "b")))
    raises("probabilities not covering exactly the options are refused",
           client.JevError, lambda: client.validate_answer(_body(probs={"a": 1.0}), ("a", "b")))
    check("a well-formed answer from the pinned model passes",
          client.validate_answer(_body(), ("a", "b"))["choice"], "a")
    session = _Session(_Resp(200, _body()))
    client.JevClient(key="k-test", session=session).ask({"title": "x"}, {"type": "choice"})
    check("the request names the pinned version, not an alias",
          session.sent[0]["json"]["model"], "jev-1.13.0")


def test_key_never_leaves_the_header() -> None:
    print("\nThe API key appears only in the Authorization header")
    secret = "tsk_SECRET_should_never_print"
    session = _Session(_Resp(401, {"error": f"invalid key {secret}"}))
    c = client.JevClient(key=secret, session=session)
    try:
        c.ask({"title": "x"}, {"type": "choice"})
        message = ""
    except client.JevError as exc:
        message = str(exc)
    check("a 401 raises without the key in its message",
          secret in message or message == "", False)
    sent = session.sent[0]
    check("the key is in the Authorization header", sent["headers"]["Authorization"],
          f"Bearer {secret}")
    check("...and nowhere in the body", secret in json.dumps(sent["json"]), False)
    check("key_status reports a word, never the key",
          client.key_status() in ("set", "missing"), True)


def test_comparator_is_production() -> None:
    print("\nThe keyword comparator is production's own matcher on production's text")
    check("KEYWORD_MATCHER is ingest.matched_competencies, by identity",
          comparator.KEYWORD_MATCHER is ingest.matched_competencies, True)
    row = {"reference_number": "X", "title": "Cloud migration",
           "description": "Move workloads to cloud", "unspsc": None, "gsin": None,
           "closing_date": None}
    check("ImputerState.text equals predicates.Notice.text",
          ImputerState.from_row(row).text, P.Notice.from_archive_row(row).text)


def test_cache_is_append_only() -> None:
    print("\nThe verdict cache and call ledger are append-only in the store")
    with tempfile.TemporaryDirectory() as tmp:
        conn = cache.connect(Path(tmp) / "c.db")
        prepared = prepare(ImputerState("t", "d"))
        verdict = {"model": "jev-1.13.0", "choice": "a", "probabilities": {"a": 1.0},
                   "confidence": 1.0, "input_tokens": 5, "output_tokens": 0}
        cache.put(conn, "N1", prepared, "q" * 64, verdict)
        cache.log_call(conn, "test", "N1", "jev-1.13.0", 5, 0, "stored")
        raises("UPDATE on verdicts aborts", sqlite3.DatabaseError,
               lambda: conn.execute("UPDATE verdicts SET choice='b'"))
        raises("DELETE on verdicts aborts", sqlite3.DatabaseError,
               lambda: conn.execute("DELETE FROM verdicts"))
        raises("DELETE on the ledger aborts", sqlite3.DatabaseError,
               lambda: conn.execute("DELETE FROM calls"))
        raises("a second verdict under the same key is refused", sqlite3.IntegrityError,
               lambda: cache.put(conn, "N1", prepared, "q" * 64, verdict))
        check("a different question hash is a miss",
              cache.get(conn, "N1", prepared.content_sha256, "jev-1.13.0", "r" * 64), None)
        conn.close()


def test_abort_logs_every_sent_call() -> None:
    print("\nAn auth failure stops the run, and every call sent is in the ledger")
    import threading

    class FakeClient:
        def __init__(self):
            self.sent = 0
            self._lock = threading.Lock()

        def ask(self, state, payload):
            with self._lock:
                self.sent += 1
            raise client.JevAuthError("HTTP 401: nope")

    fake = FakeClient()
    q = Q.Question((), {"type": "choice"}, "q" * 64)
    blind = [(f"N{i}", ImputerState(f"t{i}", "d")) for i in range(60)]
    with tempfile.TemporaryDirectory() as tmp:
        conn = cache.connect(Path(tmp) / "c.db")
        raises("a 401 aborts the run", client.JevAuthError,
               lambda: evaluate.impute(blind, q, "test", fake, conn, workers=4,
                                       echo=lambda *_: None))
        logged = conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
        check("every call that was sent is logged", logged, fake.sent)
        check("...and the run stopped long before 60", fake.sent < 60, True)
        conn.close()


def _imports(source: str) -> list:
    names = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    return names


# What Claude reads: the tools, the MCP server, and the skills that drive the
# briefing and the pre-mortem. None of it may reach a probability.
CLAUDE_READ_PY = (lambda: list((SCRIPTS / "tender_tools").rglob("*.py"))
                  + [SCRIPTS / "mcp_server.py"])
CLAUDE_READ_MD = (lambda: list((Path(__file__).parent.parent / ".claude" / "skills")
                               .rglob("*.md")))
# Names that carry, compute or locate the probability. Absent from every
# Claude-read surface as identifiers, attributes or string literals.
PROBABILITY_TOKENS = ("imputed_mass", "p_profile", "IMPUTER_THRESHOLD", "Imputation",
                      "family_imputer_gate", "probabilities", "family_imputer",
                      # ref-007: the flag store, its band, and the names that
                      # compute or locate them. The store is committed, so the
                      # scan is what keeps Claude's tools from reading it.
                      "coded_flags", "mass_band", "CodedFlag", "coded_flag",
                      "FLAG_THRESHOLD", "flag_store", "CODED_FLAGS")


def test_import_boundary() -> None:
    print("\nOnly ingest/cli.py imports the imputer, and only its gate (ref-006)")
    product = (CLAUDE_READ_PY() + list((SCRIPTS / "ingest").rglob("*.py"))
               + [SCRIPTS / "filter_audit" / "predicates.py"])
    allowed = {("cli.py", "ingest"): {"family_imputer.gate"}}
    offenders = []
    for path in product:
        mods = [m for m in _imports(path.read_text(encoding="utf-8"))
                if m.split(".")[0] == "family_imputer"]
        ok = allowed.get((path.name, path.parent.name), set())
        offenders += [f"{path.parent.name}/{path.name}: imports {m}"
                      for m in mods if m not in ok]
    check(f"{len(product)} product modules scanned; only ingest/cli.py -> "
          f"family_imputer.gate", offenders, [])
    cli_mods = [m for m in _imports((SCRIPTS / "ingest" / "cli.py").read_text(encoding="utf-8"))
                if m.startswith("family_imputer")]
    check("...and ingest/cli.py does import exactly the gate", cli_mods,
          ["family_imputer.gate"])


def _probability_hits(source: str) -> list:
    hits = []
    for node in ast.walk(ast.parse(source)):
        text = (node.id if isinstance(node, ast.Name) else
                node.attr if isinstance(node, ast.Attribute) else
                node.value if isinstance(node, ast.Constant) and isinstance(node.value, str)
                else None)
        if text and any(t in text for t in PROBABILITY_TOKENS):
            hits.append(f"{node.lineno} {text[:40]!r}")
    return hits


def test_probability_never_reaches_claude() -> None:
    print("\nNo Claude-read surface names, computes or locates the probability")
    planted = ("def show(meta):\n    return meta.get('imputed_mass')\n"
               "x = row.p_profile\n")
    check("the scan catches a planted leak (not a vacuous pass)",
          len(_probability_hits(planted)), 2)
    planted_flags = ("import json\nrows = open('data/coded_flags.jsonl')\n"
                     "band = rows[0]['mass_band']\n")
    check("...including a planted read of the ref-007 flag store and its band",
          len(_probability_hits(planted_flags)), 2)
    offenders = []
    for path in CLAUDE_READ_PY():
        offenders += [f"{path.parent.name}/{path.name}:{h}"
                      for h in _probability_hits(path.read_text(encoding="utf-8"))]
        offenders += [f"{path.name}: imports {m}" for m in _imports(path.read_text(encoding="utf-8"))
                      if m.startswith("filter_audit.predicates") or m.startswith("family_imputer")]
    for path in CLAUDE_READ_MD():
        text = path.read_text(encoding="utf-8")
        offenders += [f"{path.parent.name}/{path.name}: {t}" for t in PROBABILITY_TOKENS
                      if t in text]
    check(f"{len(CLAUDE_READ_PY())} tool modules and {len(CLAUDE_READ_MD())} skill "
          f"files carry none of {len(PROBABILITY_TOKENS)} probability names", offenders, [])

    from ingest.corpus import RELEVANCE_METADATA_KEYS, relevance_metadata
    check("the corpus relevance keys are exactly basis, family, model",
          RELEVANCE_METADATA_KEYS, ("relevance_basis", "imputed_family", "imputer_model"))
    row = {"_relevance_basis": "imputed", "_imputed_family": "8111",
           "_imputer_model": "jev-1.13.0", "_imputed_mass": 0.91, "p_profile": 0.91}
    meta = relevance_metadata(row)
    check("an imputed row writes only the allowed keys, even when the row "
          "carries a mass", sorted(meta), sorted(RELEVANCE_METADATA_KEYS))
    check("...and no value in it is a number", any(isinstance(v, float) for v in meta.values()),
          False)
    check("a coded row writes only its basis",
          relevance_metadata({"_relevance_basis": "unspsc"}), {"relevance_basis": "unspsc"})
    corpus_src = (SCRIPTS / "ingest" / "corpus.py").read_text(encoding="utf-8")
    consts = [n.value for n in ast.walk(ast.parse(corpus_src))
              if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    check("corpus.py holds no string literal naming a mass or probability",
          [c for c in consts if "mass" in c.lower() or "probab" in c.lower()], [])


def test_imputed_fields_are_withheld() -> None:
    print("\nImputed family and probability are withheld from the blinded review")
    wanted = {"imputed_family", "imputed_family_probability", "imputed_family_distribution"}
    check("all three are in WITHHELD_UNTIL_DISPOSED",
          wanted <= set(blinding.WITHHELD_UNTIL_DISPOSED), True)
    check("...and none is on BlindedNotice",
          wanted & {f.name for f in fields(blinding.BlindedNotice)}, set())
    raises("assert_blinded refuses a payload carrying one", AssertionError,
           lambda: blinding.assert_blinded({"title": "x", "imputed_family": "8111"}))
    flag_fields = {"coded_flag", "jev_choice", "mass_band"}
    check("ref-007's flag, choice and band are withheld too",
          flag_fields <= set(blinding.WITHHELD_UNTIL_DISPOSED), True)
    check("...and none is on BlindedNotice",
          flag_fields & {f.name for f in fields(blinding.BlindedNotice)}, set())


def test_label_writes_only_the_scratch_file() -> None:
    print("\nlabel refuses unsampled ids and writes only to its JSONL")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        report = tmp / "report.json"
        labels = tmp / "labels.jsonl"
        report.write_text(json.dumps({
            "model": "jev-1.13.0", "question_sha256": "q" * 64,
            "disagreement_sample": [{"notice_id": "WS1",
                                     "direction": "jev_admits_publisher_did_not"}]}))
        raises("an id outside the printed sample is refused", ValueError,
               lambda: evaluate.record_label("WS2", "jev_wrong", "", labelled_by="human",
                                             report_path=report, labels_path=labels))
        raises("a kind outside the two is refused", ValueError,
               lambda: evaluate.record_label("WS1", "ambiguous", "", labelled_by="human",
                                             report_path=report, labels_path=labels))
        evaluate.record_label("WS1", "publisher_miscoded", "n", labelled_by="human", report_path=report,
                              labels_path=labels)
        lines = labels.read_text().splitlines()
        check("one line appended", len(lines), 1)
        check("...recording kind and direction",
              (json.loads(lines[0])["kind"], json.loads(lines[0])["direction"]),
              ("publisher_miscoded", "jev_admits_publisher_did_not"))
    default = evaluate.LABELS_JSONL.resolve()
    reviews = (Path(__file__).parent.parent / "vault" / "reference").resolve()
    check("the default labels path is outside vault/reference",
          reviews in default.parents, False)


def test_decision_rule() -> None:
    print("\nThe pre-registered rule, including the absolute floor")
    def m(r, p):
        return {"recall": {"value": r}, "precision": {"value": p}}
    check("clear win -> PROCEED", evaluate.decide(m(0.80, 0.70), m(0.50, 0.70))["outcome"], "PROCEED")
    check("+10 pts over a weak comparator but under the 0.60 floor -> not PROCEED",
          evaluate.decide(m(0.40, 0.70), m(0.30, 0.70))["outcome"], "INCONCLUSIVE")
    check("no recall gain -> KILL", evaluate.decide(m(0.50, 0.90), m(0.50, 0.70))["outcome"], "KILL")
    check("precision collapse -> KILL", evaluate.decide(m(0.90, 0.50), m(0.50, 0.70))["outcome"], "KILL")
    check("undefined precision -> NOT EVALUATED",
          evaluate.decide(m(0.9, None), m(0.5, 0.7))["outcome"], "NOT EVALUATED")


def test_three_label_kinds_and_the_sheet() -> None:
    print("\nThree label kinds; the reading sheet matches the report and pre-fills nothing")
    check("the kinds are exactly the six", evaluate.LABEL_KINDS,
          ("jev_wrong", "publisher_miscoded", "out_of_scope",
           "profile_gap", "no_description", "unsure"))
    check("every kind has a definition",
          all(evaluate.LABEL_DEFINITIONS[k].strip() for k in evaluate.LABEL_KINDS), True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        ref = O.load_reference(_fake_reference(tmp))
        check("an L4 code reads as itself",
              O.describe_code("80101507", ref), "Information technology consultation services")
        check("a class-level code reads at its class",
              O.describe_code("80101500", ref), "Business consultation")
        check("an unknown commodity is not read as its parent",
              O.describe_code("80101599", ref), "(not in the PSPC reference file)")

        report = tmp / "report.json"
        labels = tmp / "labels.jsonl"
        sheet = tmp / "sheet.md"
        report.write_text(json.dumps({
            "model": "jev-1.13.0", "question_sha256": "q" * 64,
            "disagreement_sample": [
                {"notice_id": "WS1", "direction": "jev_admits_publisher_did_not"},
                {"notice_id": "cb-2", "direction": "publisher_admits_jev_did_not"}]}))
        evaluate.record_label("WS1", "out_of_scope", "", labelled_by="human", report_path=report, labels_path=labels)
        check("out_of_scope is accepted by label",
              json.loads(labels.read_text().splitlines()[0])["kind"], "out_of_scope")

        def row(nid, direction, truncated=False):
            return {"notice_id": nid, "direction": direction, "title": f"Title {nid}",
                    "description": "Line one\n\nLine two", "codes": ["80101507"],
                    "choice": "a", "choice_p": 0.6,
                    "probabilities": {"a": 0.6, "b": 0.3, "c": 0.1},
                    "kw_hits": [], "truncated": truncated}
        sample = [row("WS1", "jev_admits_publisher_did_not"),
                  row("cb-2", "publisher_admits_jev_did_not", truncated=True)]
        raises("a sample in a different order than the report is refused",
               evaluate.SampleDrift,
               lambda: evaluate.write_label_sheet(list(reversed(sample)), ref,
                                                  path=sheet, report_path=report))
        evaluate.write_label_sheet(sample, ref, path=sheet, report_path=report)
        text = sheet.read_text(encoding="utf-8")
        check("each notice id is a heading, in report order",
              [l[3:] for l in text.splitlines() if l.startswith("## ") and l[3:] in ("WS1", "cb-2")],
              ["WS1", "cb-2"])
        check("all three definitions are at the top",
              all(f"`{k}`" in text.split("## WS1")[0] for k in evaluate.LABEL_KINDS), True)
        check("every label and note line is blank",
              {l for l in text.splitlines() if l.startswith(("label:", "note:"))},
              {"label:", "note:"})
        check("the next two options are shown",
              ("next: b (p = 0.30)" in text, "next: c (p = 0.10)" in text), (True, True))
        check("no-keyword notices read 'none'", "**Keyword hits:** none" in text, True)
        check("the full description is carried, not an excerpt",
              "> Line one" in text and "> Line two" in text, True)
        check("the code carries its English description",
              "`80101507` Information technology consultation services" in text, True)


def _sheet(blocks) -> str:
    """A reviewed sheet: header prose, then (id, label, why, note) blocks, then findings."""
    out = ["# Disagreement sample - reading sheet", "", "## Label kinds", "",
           "- label: this line is in the header and must not be read", ""]
    for i, (nid, label, why, note) in enumerate(blocks, 1):
        out += ["---", "", f"## {nid}", "", f"**{i:02} of {len(blocks)}**: Jev admits",
                "", "**Description:**", "", "> label: jev_wrong inside a quote is not a label",
                "", f"label: {label}", ""]
        if why is not None:
            out += [f"**Why unsure:** {why}", ""]
        out += [f"note: {note}", ""]
    out += ["# Findings from the reading", "", "## A fourth label kind is missing", "",
            "label: prose here is not a block"]
    return "\n".join(out)


def test_label_ingest() -> None:
    print("\nIngest: validate everything, write nothing on any problem, never twice")
    import inspect
    check("record_label has no default labeller",
          inspect.signature(evaluate.record_label).parameters["labelled_by"].default,
          inspect.Parameter.empty)
    from family_imputer.cli import main as cli_main
    try:
        cli_main(["label", "WS1", "jev_wrong"])
        refused = False
    except SystemExit as exc:
        refused = exc.code == 2
    check("the label command refuses without --labelled-by", refused, True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        report = tmp / "report.json"
        report.write_text(json.dumps({
            "model": "jev-1.13.0", "question_sha256": "q" * 64,
            "disagreement_sample": [
                {"notice_id": "WS1", "direction": "jev_admits_publisher_did_not"},
                {"notice_id": "cb-2", "direction": "publisher_admits_jev_did_not"}]}))
        good = [("WS1", "publisher_miscoded", None, "a note"),
                ("cb-2", "unsure", "profile_gap or jev_wrong: turns on X", "n2")]

        def attempt(label, blocks):
            labels = tmp / f"{label}.jsonl"
            sheet = tmp / f"{label}.md"
            sheet.write_text(_sheet(blocks), encoding="utf-8")
            try:
                evaluate.ingest_label_sheet(sheet, "human", report_path=report,
                                            labels_path=labels)
                refused = False
            except evaluate.LabelSheetError:
                refused = True
            check(f"{label}: refused and nothing written",
                  (refused, labels.exists()), (True, False))

        attempt("missing id", good[:1])
        attempt("extra id", good + [("WS9", "jev_wrong", None, "x")])
        attempt("duplicate id", good + [good[0]])
        attempt("unknown kind", [good[0], ("cb-2", "maybe", None, "x")])
        attempt("unsure with no reason", [good[0], ("cb-2", "unsure", None, "x")])
        attempt("two label lines", [good[0], ("cb-2", "jev_wrong\nlabel: unsure", None, "x")])

        labels = tmp / "ok.jsonl"
        sheet = tmp / "ok.md"
        sheet.write_text(_sheet(good), encoding="utf-8")
        recs = evaluate.ingest_label_sheet(sheet, "human", report_path=report,
                                           labels_path=labels)
        check("a clean sheet records every block, in sample order",
              [(r["notice_id"], r["kind"]) for r in recs],
              [("WS1", "publisher_miscoded"), ("cb-2", "unsure")])
        check("quoted and header 'label:' lines are not read", len(recs), 2)
        check("records carry labeller, reason and the sheet's hash",
              (recs[1]["labelled_by"], recs[1]["why_unsure"],
               len(recs[1]["source"]["sha256"])),
              ("human", "profile_gap or jev_wrong: turns on X", 64))
        raises("re-ingesting the same sheet is refused", evaluate.LabelSheetError,
               lambda: evaluate.ingest_label_sheet(sheet, "human", report_path=report,
                                                   labels_path=labels))
        check("...and wrote nothing more", len(labels.read_text().splitlines()), 2)
        check("an assistant reading of the same notices is a separate record",
              len(evaluate.ingest_label_sheet(sheet, "assistant", report_path=report,
                                              labels_path=labels)), 2)
        table = evaluate.label_table(evaluate.load_labels(labels))
        check("the table keeps labellers apart", sorted(table), ["assistant", "human"])


def test_strip_and_ceiling() -> None:
    print("\nVariant B stripper and input-ceiling flags")
    from family_imputer import ceiling as C
    from family_imputer import strip as S
    check("strip rules match their recorded hash", S.rules_sha256(), S.STRIP_RULES_SHA256)
    names = "\n".join(f"{i}.\tVendor Number {i} Inc." for i in range(1, 16))
    desc = ("The requirement is for a systems administrator.\n"
            "The following SA Holders have been invited:\n" + names +
            "\nFile Number: R1")
    s = S.strip_supplier_lists(desc)
    check("a 15-name numbered list is removed as one run", (len(s.runs), s.names_removed), (1, 15))
    check("...and the prose on either side is kept",
          ("systems administrator" in s.text, "File Number: R1" in s.text,
           "[invited-supplier list: 15 names removed]" in s.text), (True, True, True))
    bullets = "\n".join(f"- Managed services item {i}" for i in range(1, 16))
    check("a bulleted requirement list with no legal suffixes is kept",
          S.strip_supplier_lists(bullets).runs, ())
    nine = "\n".join(f"Vendor {i} Inc." for i in range(9))
    check("nine names is below the run threshold", S.strip_supplier_lists(nine).runs, ())

    ariba = ("AMENDMENT TO CLOSING TIME: Please disregard the Ariba Discovery posting "
             "response deadline closing time and CanadaBuys posting closing time. All "
             "required supporting documentation and proposals must be submitted by "
             "January 22, 2024 at 2:00 PM EST. All proposals submitted after 2:00 PM EST "
             "will result in the proposal being declared non-responsive. The period of "
             "the contract is from date of contract award, up to 3 years.")
    f = C.flags(ariba)
    check("Ariba boilerplate plus a contract period is boilerplate_only",
          (f["short"], f["boilerplate_only"]), (False, True))
    work = ariba.replace("The period of the contract",
                         "The Giant Mine Remediation Project requires a contractor to develop "
                         "Version 1 of the Perpetual Care Plan for the Giant Mine in "
                         "Yellowknife, Northwest Territories, including long-term monitoring "
                         "and maintenance planning. The period of the contract")
    check("the same boilerplate around a described requirement is not flagged",
          C.flags(work)["boilerplate_only"], False)
    check("'See Attached.' is short", C.flags("See Attached.")["short"], True)


def _feed_frame():
    """Six feed rows. Coded: one its codes admit, one they reject, one they
    reject that has closed. Uncoded: three, of which only one survives
    closed/exclusion/construction/jurisdiction and reaches relevance."""
    import pandas as pd
    base = {"contractingEntityName-nomEntitContractante-eng": "Shared Services Canada (SSC)",
            "endUserEntitiesName-nomEntitesUtilisateurFinal-eng": "",
            "noticeType-avisType-eng": "Request for Proposal",
            "procurementCategory-categorieApprovisionnement": "*SRV",
            "tenderClosingDate-appelOffresDateCloture": "2026-12-01T14:00:00",
            "unspsc": ""}
    rows = [
        dict(base, **{"referenceNumber-numeroReference": "CODED-1", "unspsc": "*81111500",
                      "title-titre-eng": "Application support services",
                      "tenderDescription-descriptionAppelOffres-eng": "Support a line-of-business application."}),
        dict(base, **{"referenceNumber-numeroReference": "CODED-REJECT", "unspsc": "*81171500",
                      "title-titre-eng": "GIS Hub maintenance",
                      "tenderDescription-descriptionAppelOffres-eng": "Maintain a spatial data platform."}),
        dict(base, **{"referenceNumber-numeroReference": "CODED-REJECT-CLOSED",
                      "unspsc": "*81171500",
                      "tenderClosingDate-appelOffresDateCloture": "2026-01-01T14:00:00",
                      "title-titre-eng": "Closed GIS work",
                      "tenderDescription-descriptionAppelOffres-eng": "Old."}),
        dict(base, **{"referenceNumber-numeroReference": "UNCODED-LIVE",
                      "title-titre-eng": "Local Internet Access Services",
                      "tenderDescription-descriptionAppelOffres-eng": "Managed internet access for regional offices."}),
        dict(base, **{"referenceNumber-numeroReference": "UNCODED-CLOSED",
                      "tenderClosingDate-appelOffresDateCloture": "2026-01-01T14:00:00",
                      "title-titre-eng": "Closed notice", "tenderDescription-descriptionAppelOffres-eng": "Old."}),
        dict(base, **{"referenceNumber-numeroReference": "UNCODED-CNST",
                      "procurementCategory-categorieApprovisionnement": "*CNST",
                      "title-titre-eng": "Roof replacement", "tenderDescription-descriptionAppelOffres-eng": "Replace a roof."}),
    ]
    df = pd.DataFrame(rows)
    cols = ingest.resolve_columns(list(df.columns), ingest.TENDER_COLUMNS,
                                  ingest.TENDER_REQUIRED, "test")
    return df, cols


class _SpyClient:
    """Stands in for JevClient. Records every call; answers with a fixed
    distribution, or raises."""
    def __init__(self, probs=None, raise_with=None):
        self.calls, self.probs, self.raise_with = [], probs, raise_with

    def ask(self, state, payload):
        self.calls.append(state)
        if self.raise_with is not None:
            raise self.raise_with
        keys = list(payload["criteria"])
        probs = {k: 0.0 for k in keys}
        probs.update(self.probs or {})
        rest = 1.0 - sum(probs.values())
        probs[keys[-1]] += rest
        top = max(probs, key=probs.get)
        return {"model": "jev-1.13.0",
                "answers": {Q.QUESTION_ID: {"type": "choice", "choice": top,
                                            "probabilities": probs, "confidence": 0.5}},
                "usage": {"input_tokens": 3500, "output_tokens": 0}}


def test_gate_in_the_ingest() -> None:
    print("\nThe ingest gate: coded never called, order enforced, mass rule, fallbacks")
    import contextlib
    import io
    from datetime import date
    from family_imputer import gate as G
    frozen = Q.load_frozen_payload()
    key_of = {o["prefix"]: o["key"] for o in frozen["options"]}
    families = ingest.parse_profile(ingest.DEFAULT_PROFILE)["unspsc_families"]
    criteria = ingest.parse_profile(ingest.DEFAULT_PROFILE)
    as_of = date(2026, 9, 24)

    def run(imputer):
        df, cols = _feed_frame()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            out = ingest.filter_tenders(df, criteria, cols, as_of=as_of, imputer=imputer)
        return out, buf.getvalue()

    seen = []

    def spy_imputer(items):
        seen.extend(items)
        return G.GateRun()
    run(spy_imputer)
    check("only the uncoded notice past gates 1-4 reaches the imputer",
          [i[0] for i in seen], ["UNCODED-LIVE"])

    with tempfile.TemporaryDirectory() as tmp:
        cache_path = Path(tmp) / "gate.db"
        # two profile options at 0.20 each, behind a 0.25 non-profile option
        split = _SpyClient({key_of["8111"]: 0.20, key_of["8116"]: 0.20, key_of["72"]: 0.25})
        imp = G.make_imputer(families, key="k", client=split, cache_path=cache_path)
        out, log = run(imp)
        # Deliberately narrowed by ref-007: coded REJECTS now go to the flagger.
        # This run has no flagger, so the imputer still sees no coded notice.
        check("with no flagger, no coded notice produces an API call: one call, "
              "for the uncoded one", len(split.calls), 1)
        check("two profile options at 0.20 behind a 0.25 non-profile option is admitted",
              "UNCODED-LIVE" in set(out[ingest.TENDER_COLUMNS["tender_id"][0]]), True)
        check("...recorded as imputed, with the top profile family",
              out.set_index(ingest.TENDER_COLUMNS["tender_id"][0])
                 .loc["UNCODED-LIVE", ["_relevance_basis", "_imputed_family"]].tolist(),
              ["imputed", "8111"])
        check("the funnel prints the imputed split", "1 imputed (1 admitted" in log, True)
        check("provenance carries the mode, with no probability in it",
              (out.attrs["relevance_mode"]["relevance_imputed"],
               any("mass" in k for k in out.attrs["relevance_mode"])), (1, False))
        again = _SpyClient({key_of["8111"]: 0.99})
        run(G.make_imputer(families, key="k", client=again, cache_path=cache_path))
        check("a re-run is served from the cache: no call", len(again.calls), 0)

    with tempfile.TemporaryDirectory() as tmp:
        # the profile option sits second at 0.02 behind a non-profile option
        low = _SpyClient({key_of["72"]: 0.98, key_of["8111"]: 0.02})
        out, _ = run(G.make_imputer(families, key="k", client=low,
                                    cache_path=Path(tmp) / "g.db"))
        check("a profile option second at 0.02 is rejected (mass 0.02 < 0.20)",
              "UNCODED-LIVE" in set(out[ingest.TENDER_COLUMNS["tender_id"][0]]), False)

    with tempfile.TemporaryDirectory() as tmp:
        spy = _SpyClient({key_of["8111"]: 1.0})
        _, log = run(G.make_imputer(families, key="", client=None,
                                    cache_path=Path(tmp) / "g.db"))
        check("no key: stands down, says so, falls back to keywords",
              ("TYPESAFE_API_KEY not set" in log, "0 imputed" in log, "1 keyword fallback" in log),
              (True, True, True))
        broken = _SpyClient(raise_with=client.JevError("HTTP 500"))
        out, log = run(G.make_imputer(families, key="k", client=broken,
                                      cache_path=Path(tmp) / "g2.db"))
        check("an API error falls back per notice and the ingest completes",
              ("1 keyword fallback" in log, "api error" in log, len(out) >= 1), (True, True, True))
        denied = _SpyClient(raise_with=client.JevAuthError("HTTP 401"))
        run(G.make_imputer(families, key="k", client=denied, cache_path=Path(tmp) / "g3.db"))
        check("an auth error is not retried per notice", len(denied.calls), 1)

        def exploding(items):
            raise RuntimeError("boom")
        out, log = run(exploding)
        check("an imputer that raises anyway never fails the ingest",
              ("imputer raised RuntimeError" in log, len(out) >= 1), (True, True))
        _, log = run(G.make_imputer(families + ["9999"], key="k", client=spy,
                                    cache_path=Path(tmp) / "g4.db"))
        check("a profile that no longer matches the frozen question stands down",
              ("profile families differ" in log, len(spy.calls)), (True, 0))
        _, log = run(None)
        check("with no imputer the split still prints, at zero",
              "Uncoded relevance: 0 imputed" in log, True)

        tampered = Path(tmp) / "fq.json"
        data = json.loads(Q.FROZEN_PAYLOAD.read_text(encoding="utf-8"))
        data["payload"]["instructions"] += " Prefer IT."
        tampered.write_text(json.dumps(data), encoding="utf-8")
        raises("an edited frozen question is refused on load", Q.FrozenQuestionDrift,
               lambda: Q.load_frozen_payload(tampered))

    n = P.Notice.from_frozen_row({"reference_number": "X", "title": "t", "description": "d",
                                  "unspsc": "*72101504", "closing_date": None})
    r = P.stage_relevance(n, {"unspsc_families": families, "competencies": []}, None,
                          imputation=P.Imputation("8111", 1.0, "jev-1.13.0", "q"))
    check("an imputation handed in for a coded notice is ignored and flagged",
          (r.outcome, r.detail["relevance_basis"], r.detail["imputation_ignored_on_coded"]),
          ("drop", "unspsc", True))

    # ref-006 defect: the float sum of 0.01-quantised probabilities lands one ulp
    # under the threshold. cb-97-2152788's profile options were 0.08/0.09/0.03.
    edge = 0.08 + 0.09 + 0.03
    check("the edge is real: 0.08 + 0.09 + 0.03 < 0.20 in floats", edge < 0.20, True)
    u = P.Notice.from_frozen_row({"reference_number": "U", "title": "t", "description": "d",
                                  "unspsc": "", "closing_date": None})
    r = P.stage_relevance(u, {"unspsc_families": families, "competencies": []}, None,
                          imputation=P.Imputation("8111", edge, "jev-1.13.0", "q"))
    check("...and a mass summing to 0.20 is admitted at t = 0.20", r.outcome, "pass")
    check("0.19 is still rejected", P.mass_reaches(0.19, 0.20), False)


def test_coded_flags() -> None:
    print("\nref-007 flags: coded rejects only, after the imputer, never admitted")
    import argparse
    import contextlib
    import io
    from datetime import date
    from family_imputer import gate as G
    from ingest import cli as ingest_cli
    from ingest import flag_store, paths
    frozen = Q.load_frozen_payload()
    key_of = {o["prefix"]: o["key"] for o in frozen["options"]}
    families = ingest.parse_profile(ingest.DEFAULT_PROFILE)["unspsc_families"]
    criteria = ingest.parse_profile(ingest.DEFAULT_PROFILE)
    tid = ingest.TENDER_COLUMNS["tender_id"][0]
    as_of = date(2026, 9, 24)

    def run(imputer, flagger):
        df, cols = _feed_frame()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            out = ingest.filter_tenders(df, criteria, cols, as_of=as_of,
                                        imputer=imputer, flagger=flagger)
        return out, buf.getvalue()

    # --- who is asked, and in what order -------------------------------------
    order = []

    def spy(name):
        def call(items):
            order.append((name, [i[0] for i in items]))
            return G.GateRun()
        return call
    run(spy("imputer"), spy("flagger"))
    check("the imputer runs first, on the uncoded notice; the flagger after, on the "
          "one coded reject past gates 1-4", order,
          [("imputer", ["UNCODED-LIVE"]), ("flagger", ["CODED-REJECT"])])

    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        uncoded_client = _SpyClient({key_of["8111"]: 0.20, key_of["8116"]: 0.20})
        # 0.83 + 0.06 + 0.01 is 0.8999999999999999 in floats: flagged at 0.90
        flag_client = _SpyClient({key_of["8111"]: 0.83, key_of["8116"]: 0.06,
                                  key_of["4323"]: 0.01})
        base, _ = run(G.make_imputer(families, key="k", client=_SpyClient(
            {key_of["8111"]: 0.20, key_of["8116"]: 0.20}), cache_path=t / "a.db"), None)
        out, log = run(G.make_imputer(families, key="k", client=uncoded_client,
                                      cache_path=t / "b.db"),
                       G.make_imputer(families, key="k", client=flag_client,
                                      cache_path=t / "b.db"))
        sent = [s["title"] for s in uncoded_client.calls + flag_client.calls]
        check("a coded notice its codes admit produces no API call from either",
              "Application support services" in sent, False)
        check("...a closed coded reject produces none either", "Closed GIS work" in sent, False)
        check("THE CORPUS IS UNCHANGED BY THE RULE: same rows, same columns, same values",
              (list(out[tid]), out.equals(base)), (list(base[tid]), True))
        check("...the flagged notice is not admitted", "CODED-REJECT" in set(out[tid]), False)
        records = out.attrs["_coded_flag_records"]
        check("one flag, at the one-ulp edge, in the lowest band",
              [(r["flag"].notice_id, r["flag"].mass_band, r["flag"].jev_choice)
               for r in records], [("CODED-REJECT", "0.90-0.95", "8111")])
        check("the funnel prints the flag count",
              "Coded flags (ref-007, flag only, none admitted): 1 of 1 coded rejects" in log, True)
        check("provenance carries counts and a status, no band and no mass",
              (out.attrs["relevance_mode"]["flags_coded"],
               any("mass" in k or "band" in k for k in out.attrs["relevance_mode"])), (1, False))

        low = _SpyClient({key_of["8111"]: 0.89})
        out, log = run(None, G.make_imputer(families, key="k", client=low,
                                            cache_path=t / "c.db"))
        check("mass 0.89 is not flagged, and the funnel says 0",
              (len(out.attrs["_coded_flag_records"]),
               "none admitted): 0 of 1 coded rejects; 1 evaluated" in log), (0, True))

        _, log = run(None, None)
        check("with no flagger the line still prints, at zero",
              "none admitted): 0 of 1 coded rejects; 0 evaluated, 0 not evaluated" in log, True)

        def exploding(items):
            raise RuntimeError("boom")
        out, log = run(G.make_imputer(families, key="k", client=_SpyClient(
            {key_of["8111"]: 0.20, key_of["8116"]: 0.20}), cache_path=t / "d.db"), exploding)
        check("a flagger that raises changes nothing: the uncoded admit stands, the run completes",
              ("UNCODED-LIVE" in set(out[tid]), "flagger raised RuntimeError" in log), (True, True))
        denied = _SpyClient(raise_with=client.JevAuthError("HTTP 401"))
        _, log = run(None, G.make_imputer(families, key="k", client=denied,
                                          cache_path=t / "e.db"))
        check("an auth error leaves the coded reject counted as not evaluated",
              "0 evaluated, 1 not evaluated" in log, True)

    # --- the pure function and its type --------------------------------------
    imp = P.Imputation("8111", 0.95, "jev-1.13.0", "q", "c")

    def notice(unspsc):
        return P.Notice.from_frozen_row({"reference_number": "N", "title": "t",
                                         "description": "d", "unspsc": unspsc,
                                         "closing_date": None})
    check("uncoded, coded admit, no imputation: no flag",
          (P.coded_flag(notice(""), criteria, imp),
           P.coded_flag(notice("*81111500"), criteria, imp),
           P.coded_flag(notice("*72101504"), criteria, None)), (None, None, None))
    check("bands at their floors", [P.mass_band(m) for m in (0.89, 0.90, 0.95, 0.99, 1.0)],
          [None, "0.90-0.95", "0.95-0.99", "0.99+", "0.99+"])
    flag_fields = {f.name for f in fields(P.CodedFlag)}
    check("CodedFlag carries no mass and no admit field",
          {n for n in flag_fields if "mass" in n and n != "mass_band" or "admit" in n}, set())

    # --- the store -----------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp) / "coded_flags.jsonl"
        check("zero flags still creates the file (CI's git add needs a path)",
              (flag_store.append([], store)["written"], store.exists()), (0, True))
        f1 = P.coded_flag(notice("*72101504"), criteria, imp)
        f2 = P.CodedFlag("M", ("72101504",), "8116", "0.99+", "jev-1.13.0", "q", "c")
        flag_store.append([flag_store.to_record(f1, "  A   title ", "fv-x", "2026-09-25")], store)
        before = store.read_bytes()
        amended = P.CodedFlag("N", f1.filed_codes, f1.jev_choice, f1.mass_band,
                              f1.model, f1.question_sha256, "different-content")
        counts = flag_store.append([flag_store.to_record(amended, "A", "fv-x", "2026-09-26"),
                                    flag_store.to_record(f2, "B", "fv-x", "2026-09-26")], store)
        check("once per notice: an amended notice is not re-recorded",
              counts, {"written": 1, "already_recorded": 1})
        check("append-only: the old file is a byte prefix of the new one",
              store.read_bytes().startswith(before), True)
        lines = [json.loads(x) for x in store.read_text(encoding="utf-8").splitlines()]
        check("a line holds exactly the ref-007 fields",
              sorted(lines[0]), sorted(["notice_id", "title", "filed_codes", "jev_choice",
                                        "mass_band", "model", "question_sha256",
                                        "content_sha256", "filter_version", "first_seen"]))
        check("...and no value in any line is a number",
              any(isinstance(v, (int, float)) for ln in lines for v in ln.values()), False)

        original = paths.CODED_FLAGS
        paths.CODED_FLAGS = Path(tmp) / "never.jsonl"
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                status = ingest_cli._record_flags(
                    [{"flag": f2, "title": "B"}],
                    argparse.Namespace(record_flags=False, profile=ingest.DEFAULT_PROFILE))
            check("without --record-flags nothing is written, and the status says why",
                  (paths.CODED_FLAGS.exists(), status.startswith("not recorded")),
                  (False, True))

            # A directory where the file should be: every write fails.
            paths.CODED_FLAGS = Path(tmp) / "a-directory"
            paths.CODED_FLAGS.mkdir()
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                status = ingest_cli._record_flags(
                    [{"flag": f2, "title": "B"}],
                    argparse.Namespace(record_flags=True, profile=ingest.DEFAULT_PROFILE))
            check("a failed write does not raise: it returns a failure marker",
                  status.startswith("write failed: "), True)
            check("...logged loudly to stderr", "CODED FLAGS NOT RECORDED" in err.getvalue(),
                  True)
            check("...and the marker is safe inside the digest's double-quoted YAML",
                  any(ch in status for ch in '"\\\n'), False)
        finally:
            paths.CODED_FLAGS = original
    digest_src = (SCRIPTS / "digest.py").read_text(encoding="utf-8")
    check("the digest frontmatter carries flag_store_status, so a gap is visible there",
          '"flag_store_status"' in digest_src, True)
    cli_src = (SCRIPTS / "ingest" / "cli.py").read_text(encoding="utf-8")
    check("...and the ingest puts it into relevance_mode, which provenance copies",
          '["flag_store_status"] = _record_flags(' in cli_src, True)


def test_flag_labelling() -> None:
    print("\nref-007 labelling: four kinds, blind via blinding.py, 1:1 controls, reveal gated")
    import contextlib
    import io
    import pandas as pd
    from family_imputer import flag_labels as F
    from ingest import flag_store

    check("the vocabulary is exactly ref-007's four kinds, in precedence order",
          F.FLAG_LABEL_KINDS, ("vehicle", "jev_wrong", "miscoded", "out_of_scope"))
    ref007 = (Path(__file__).parent.parent / "vault" / "reference" / "filter-refinements"
              / "ref-007-coded-imputer-flags.md").read_text(encoding="utf-8")
    vocab = ref007.split("## Label vocabulary", 1)[1].split("\n## ", 1)[0]
    check("...and ref-007's vocabulary section defines the same kinds, in the same order",
          re.findall(r"^\d\. \*\*`([a-z_]+)`\*\*", vocab, re.M), list(F.FLAG_LABEL_KINDS))
    check("unassigned is not a kind", F.UNASSIGNED in F.FLAG_LABEL_KINDS, False)

    import subprocess
    from ingest import paths as ingest_paths
    check("dispositions live beside the committed flag store",
          ingest_paths.CODED_FLAG_LABELS.parent, ingest_paths.CODED_FLAGS.parent)
    ignored = [subprocess.run(["git", "check-ignore", "-q", str(p)],
                              cwd=Path(__file__).parent.parent).returncode
               for p in (ingest_paths.CODED_FLAG_LABELS, ingest_paths.CODED_FLAGS)]
    check("...and git ignores neither file (check-ignore exits 1: no match)", ignored, [1, 1])

    base = {"contractingEntityName-nomEntitContractante-eng": "Shared Services Canada (SSC)",
            "endUserEntitiesName-nomEntitesUtilisateurFinal-eng": "",
            "noticeType-avisType-eng": "Request for Proposal",
            "procurementCategory-categorieApprovisionnement": "*SRV",
            "tenderClosingDate-appelOffresDateCloture": "2026-12-01T14:00:00",
            "unspsc": "*81171500"}

    def row(nid, title, **kw):
        return dict(base, **{"referenceNumber-numeroReference": nid, "title-titre-eng": title,
                             "tenderDescription-descriptionAppelOffres-eng": f"About {title}."},
                    **kw)
    rows = [row("FLAG-A", "GIS hub"), row("FLAG-B", "Data platform"),
            row("CTRL-1", "Soil survey"), row("CTRL-2", "Lab reagents"),
            row("CTRL-3", "Fish counts"), row("ADMIT", "App support", unspsc="*81111500")]

    def flag(nid, band):
        return P.CodedFlag(nid, ("81171500",), "4323", band, "jev-1.13.0", "q", "c")

    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        feed = t / "tenders.csv"
        pd.DataFrame(rows).to_csv(feed, index=False)
        (t / "tenders.csv.http.json").write_text('{"fetched_at": "2026-09-24T12:00:00"}')
        store = t / "coded_flags.jsonl"
        flag_store.append([flag_store.to_record(flag("FLAG-A", "0.99+"), "GIS hub", "fv", "2026-09-25"),
                           flag_store.to_record(flag("FLAG-B", "0.95-0.99"), "Data platform",
                                                "fv", "2026-09-26")], store)
        kw = dict(store_path=store, feed_path=feed, feed_paths=[feed],
                  sheet_path=t / "sheet.md", queue_path=t / "queue.json",
                  labels_path=t / "labels.jsonl", ref={})

        queue = F.write_sheet(**kw)
        roles = Counter(it["role"] for it in queue["items"])
        check("two flags mixed 1:1 with two controls", (roles["flag"], roles["control"]), (2, 2))
        check("controls are unflagged coded rejects only (never the admit or a flag)",
              {it["notice_id"] for it in queue["items"] if it["role"] == "control"}
              <= {"CTRL-1", "CTRL-2", "CTRL-3"}, True)
        sheet = kw["sheet_path"].read_text(encoding="utf-8")
        # Blocks only: the header defines jev_wrong by listing the profile
        # families, which a choice like 4323 is one of.
        blocks = re.split(r"^## (?=\S+\n\n\*\*\d\d of)", sheet, flags=re.M)[1:]
        check("one block per notice, headed by its notice id",
              sorted(b.split("\n", 1)[0] for b in blocks),
              sorted(it["notice_id"] for it in queue["items"]))
        check("no band and no model choice appear in any block",
              [s for s in ("0.99+", "0.95-0.99", "4323") if any(s in b for b in blocks)], [])
        check("...both are marked withheld on every block",
              sheet.count(F.WITHHELD_TEXT), 2 * len(blocks))
        check("...and no block says whether it is a flag or a control",
              any("control" in b.lower() or "role" in b.lower() for b in blocks), False)
        check("each block carries title, filed codes, blank label: and note:",
              all(("**Title:**" in b and "`81171500`" in b and "\nlabel:\n" in b
                   and "\nnote:\n" in b) for b in blocks), True)

        # The render goes through blinding.assert_blinded, not a copy of it.
        real = blinding.blind

        class Leaky:
            def __init__(self, inner):
                self.inner = inner

            def to_dict(self):
                return dict(self.inner.to_dict(), mass_band="0.99+")
        blinding.blind = lambda item_id, row: Leaky(real(item_id, row))
        try:
            raises("a payload carrying the band is refused by blinding.assert_blinded",
                   AssertionError, lambda: F.render_sheet(queue, {}, {}))
        finally:
            blinding.blind = real

        ids = [b.split("\n", 1)[0] for b in blocks]

        def filled(labels):
            text = sheet
            for nid, (label, note) in labels.items():
                head, rest = text.split(f"## {nid}\n", 1)
                block, tail = (rest.split("\n---\n", 1) + [""])[:2]
                block = block.replace("\nlabel:\n", f"\nlabel: {label}\n", 1).replace(
                    "\nnote:\n", f"\nnote: {note}\n", 1)
                text = head + f"## {nid}\n" + block + ("\n---\n" + tail if tail else "")
            return text

        answers = {ids[0]: ("miscoded", "GIS work filed as biology"),
                   ids[1]: ("", "no description to judge"),
                   ids[2]: ("", ""),
                   ids[3]: ("unsure", "")}
        bad = t / "bad.md"
        bad.write_text(filled(answers), encoding="utf-8")
        raises("a label outside the four kinds is refused", F.FlagSheetError,
               lambda: F.ingest_sheet(bad, "human", queue_path=kw["queue_path"],
                                      labels_path=kw["labels_path"]))
        check("...and nothing was written", kw["labels_path"].exists(), False)
        stray = t / "stray.md"
        stray.write_text(filled(dict(answers, **{ids[3]: ("vehicle", "")}))
                         + "\n---\n\n## NOT-ON-SHEET\n\n**05 of 4**\n\nlabel: vehicle\n\nnote:\n",
                         encoding="utf-8")
        raises("an id not on the sheet is refused", F.FlagSheetError,
               lambda: F.ingest_sheet(stray, "human", queue_path=kw["queue_path"],
                                      labels_path=kw["labels_path"]))

        good = t / "good.md"
        good.write_text(filled(dict(answers, **{ids[3]: ("out_of_scope", "IT audit")})),
                        encoding="utf-8")
        out = F.ingest_sheet(good, "human", queue_path=kw["queue_path"],
                             labels_path=kw["labels_path"])
        check("three dispositions (one unassigned with its reason); the blank block skipped",
              out, {"written": 3, "skipped_unread": 1, "unassigned": 1})
        recorded = F.load_labels(kw["labels_path"])
        role_of = {it["notice_id"]: it["role"] for it in queue["items"]}
        check("each committed disposition carries its role, so it reads without the queue",
              [r["role"] for r in recorded], [role_of[r["notice_id"]] for r in recorded])
        raises("a second disposition by the same labeller is refused: the first stands",
               F.FlagSheetError, lambda: F.ingest_sheet(good, "human",
                                                        queue_path=kw["queue_path"],
                                                        labels_path=kw["labels_path"]))
        raises("reveal without a disposition is refused by blinding.RevealRefused",
               blinding.RevealRefused,
               lambda: F.reveal({"notice_id": ids[2], "role": "flag"}, None, {}))
        shown = F.write_revealed("human", queue_path=kw["queue_path"],
                                 labels_path=kw["labels_path"], store_path=store,
                                 out_path=t / "revealed.md")
        check("reveal shows the three labelled and withholds the unread one",
              (shown["revealed"], shown["withheld"]), (3, 1))
        revealed = (t / "revealed.md").read_text(encoding="utf-8")
        labelled_flags = [i for i in (ids[0], ids[1], ids[3]) if i.startswith("FLAG")]
        check("...every revealed flag shows its choice and band, and no control does",
              revealed.count("model's choice 4323, band "), len(labelled_flags))
        check("...and the unread notice is not in it", f"## {ids[2]}" in revealed, False)
        raises("a new sheet is refused while the current one has an unlabelled notice",
               F.FlagSheetError, lambda: F.write_sheet(**kw))

        flag_store.append([flag_store.to_record(flag(f"FLAG-{i}", "0.99+"), "x", "fv",
                                                "2026-09-27") for i in range(5)], store)
        raises("a queue that cannot be matched 1:1 with controls is refused",
               F.FlagSheetError, lambda: F.write_sheet(**dict(kw, labels_path=t / "none.jsonl"),
                                                       replace=True))


def test_sweep_definitions() -> None:
    print("\nPhase 2 sweep: low tail present; 'neither' means both methods missed")
    from family_imputer import sweep as S
    check("the grid reaches below 0.05", min(S.THRESHOLDS), 0.005)
    rows = [
        {"notice_id": "a", "publisher_admit": True, "p_profile": 0.0, "kw_admit": False},
        {"notice_id": "b", "publisher_admit": True, "p_profile": 0.0, "kw_admit": True},
        {"notice_id": "c", "publisher_admit": True, "p_profile": 0.06, "kw_admit": False},
        {"notice_id": "d", "publisher_admit": False, "p_profile": 0.0, "kw_admit": False},
    ]
    check("neither = publisher admit, mass below t, no keyword",
          [r["notice_id"] for r in S.neither(rows, 0.05)], ["a"])
    check("mass histogram counts every miss once",
          sum(b["n"] for b in S.mass_histogram(rows)), len(rows))


def test_wilson() -> None:
    print("\nWilson interval sanity")
    lo, hi = evaluate.wilson(50, 100)
    check("50/100 brackets 0.5", lo < 0.5 < hi, True)
    check("0/0 is undefined, not zero", evaluate.wilson(0, 0), None)


def main() -> int:
    test_state_is_blind()
    test_truncation_is_recorded()
    test_gsin_never_loads()
    test_options_and_mapping()
    test_question_is_frozen()
    test_model_is_pinned_and_answer_validated()
    test_key_never_leaves_the_header()
    test_comparator_is_production()
    test_cache_is_append_only()
    test_abort_logs_every_sent_call()
    test_import_boundary()
    test_probability_never_reaches_claude()
    test_imputed_fields_are_withheld()
    test_label_writes_only_the_scratch_file()
    test_decision_rule()
    test_three_label_kinds_and_the_sheet()
    test_label_ingest()
    test_strip_and_ceiling()
    test_gate_in_the_ingest()
    test_coded_flags()
    test_flag_labelling()
    test_sweep_definitions()
    test_wilson()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("All family-imputer checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

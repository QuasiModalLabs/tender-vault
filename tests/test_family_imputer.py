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
import sqlite3
import sys
import tempfile
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


def test_nothing_in_the_product_reaches_the_imputer() -> None:
    print("\nNo product surface imports family_imputer or names its data")
    targets = (list((SCRIPTS / "tender_tools").rglob("*.py"))
               + [SCRIPTS / "mcp_server.py"]
               + list((SCRIPTS / "ingest").rglob("*.py"))
               + [SCRIPTS / "filter_audit" / "predicates.py"])
    offenders = []
    for path in targets:
        source = path.read_text(encoding="utf-8")
        if "family_imputer" in source:
            offenders.append(f"{path.name}: names family_imputer")
            continue
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(n.split(".")[0] == "family_imputer" for n in names):
                offenders.append(f"{path.name}: imports it")
    check(f"{len(targets)} product modules scanned; none reaches the imputer", offenders, [])


def test_imputed_fields_are_withheld() -> None:
    print("\nImputed family and probability are withheld from the blinded review")
    wanted = {"imputed_family", "imputed_family_probability", "imputed_family_distribution"}
    check("all three are in WITHHELD_UNTIL_DISPOSED",
          wanted <= set(blinding.WITHHELD_UNTIL_DISPOSED), True)
    check("...and none is on BlindedNotice",
          wanted & {f.name for f in fields(blinding.BlindedNotice)}, set())
    raises("assert_blinded refuses a payload carrying one", AssertionError,
           lambda: blinding.assert_blinded({"title": "x", "imputed_family": "8111"}))


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
               lambda: evaluate.record_label("WS2", "jev_wrong", "",
                                             report_path=report, labels_path=labels))
        raises("a kind outside the two is refused", ValueError,
               lambda: evaluate.record_label("WS1", "ambiguous", "",
                                             report_path=report, labels_path=labels))
        evaluate.record_label("WS1", "publisher_miscoded", "n", report_path=report,
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
    test_nothing_in_the_product_reaches_the_imputer()
    test_imputed_fields_are_withheld()
    test_label_writes_only_the_scratch_file()
    test_decision_rule()
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

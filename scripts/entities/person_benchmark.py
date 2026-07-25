#!/usr/bin/env python3
"""
Person extraction benchmark — compares regex vs CRF on agenda texts
where person names are known to be present.

Usage:
    PYTHONPATH=scripts .venv/bin/python scripts/entities/person_benchmark.py
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from collections import Counter
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

from sqlalchemy import text

from db.core import get_engine
from entities.extract import KNOWN_ORGANIZATIONS, normalize_name

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("person_benchmark")


# ── Known surname gazetteer (built from pz_item_details data + common) ──
KNOWN_SURNAMES = {
    "smith", "johnson", "williams", "brown", "jones", "garcia",
    "miller", "davis", "rodriguez", "martinez", "anderson", "taylor",
    "thomas", "moore", "jackson", "martin", "lee", "thompson",
    "white", "harris", "clark", "lewis", "robinson", "walker",
    "young", "allen", "king", "wright", "scott", "hill", "green",
    "adams", "baker", "nelson", "carter", "mitchell", "roberts",
    "turner", "phillips", "campbell", "parker", "evans", "edwards",
    "collins", "stewart", "sanchez", "morris", "rogers", "reed",
    "cook", "morgan", "bell", "murphy", "bailey", "rivera",
    "cooper", "richardson", "cox", "howard", "ward", "brooks",
    "gray", "james", "watson", "burns", "hayes", "cole", "west",
    "meyer", "webb", "schmidt", "wagner", "foster", "hart",
    "fox", "berry", "perez", "owen", "coleman", "edwards",
    "hughes", "ross", "gomez", "murray", "freeman", "wells",
    "webb", "simpson", "cummings", "bates", "fisher",
    "hunter", "weber", "duncan", "lopez", "gonzalez",
    "alexander", "russell", "griffin", "diaz", "hayes",
    # Frequent names from our data
    "gilmore", "riddell", "baugh", "lake", "schube",
    "lally", "nichter", "karmazin", "furlow", "alleman",
    "powers", "walter", "quarles", "prutt", "buschbacher",
    "eichorn", "lorentzen", "schlimm", "martell", "cannon",
    "landis", "jaramillo", "applegate", "perez", "mueller",
    "gerard", "sarkissian", "holguin", "astle", "daoust",
    "ripson", "sanks", "marsh", "hill", "frome", "yancey",
    "reichenberg", "behie", "campbell", "fuller", "lin",
    "holguin", "leake", "powers", "waldier", "olberholtzer",
    "yancey", "klucznik", "crossby", "murphy", "oconnor",
    "velazquez", "vaccaro", "vermillion", "peiffer", "hale",
    "di_martino", "berry", "farr",
}

KNOWN_SURNAME_PATTERN = re.compile(
    r"\b(" + "|".join(sorted(KNOWN_SURNAMES, key=len, reverse=True)) + r")\b", re.IGNORECASE
)


def looks_like_person(name: str) -> bool:
    """Improved person-name heuristic with gazetteer support."""
    name = name.strip().rstrip(",:;.")
    if not name or len(name) < 3:
        return False
    words = name.split()
    if len(words) < 2 or len(words) > 5:
        return False
    # All words must start capital (or be single uppercase)
    if not all(w[0].isupper() for w in words if w):
        return False
    # Filter known non-person patterns
    name_lower = name.lower()
    firm_keywords = {
        "llc", "plc", "pllc", "pc", "pa", "inc", "corp", "ltd",
        "architecture", "engineering", "planning", "consulting",
        "group", "associates", "partners", "law", "firm",
        "development", "properties", "investments", "holdings",
        "company", "co", "llp", "pc",
        "landscape", "surveying", "corporation", "incorporated", "limited",
        "services", "solutions", "management",
    }
    for kw in firm_keywords:
        if kw in name_lower:
            return False
    # Must contain at least one known surname OR look like a standard 2-name
    has_surname = bool(KNOWN_SURNAME_PATTERN.search(name_lower))
    if has_surname:
        return True
    # Without gazetteer match, require 2 words both 2+ chars
    if len(words) == 2 and all(len(w) >= 2 for w in words):
        return True
    return False


# ═════════════════════════════════════════════════════════════════════
#  Regex-based person extraction (improved)
# ═════════════════════════════════════════════════════════════════════

ROLE_PATTERNS = {
    "Applicant": re.compile(
        r"(?:Applicant|Applicant/Owner|Applicant/Agent|Petitioner)\s*:?\s*(.+?)(?:\n|$)",
        re.IGNORECASE | re.MULTILINE,
    ),
    "Attorney": re.compile(
        r"(?:Attorney|Represented by|Represented By|Counsel)\s*:?\s*(.+?)(?:\n|$)",
        re.IGNORECASE | re.MULTILINE,
    ),
    "Planner": re.compile(
        r"(?:Planner|Planning Consultant|Planning Firm|Architect|Engineer)\s*:?\s*(.+?)(?:\n|$)",
        re.IGNORECASE | re.MULTILINE,
    ),
    "Presenter": re.compile(
        r"(?:Presented by|Presented By|Staff Contact|Staff|Requested by)\s*:?\s*(.+?)(?:\n|$)",
        re.IGNORECASE | re.MULTILINE,
    ),
}

GENERIC_NAME = re.compile(
    r"\b([A-Z][a-z]{1,20} [A-Z][a-z]{1,20}(?:\s[A-Z][a-z]{1,20})?)\b"
)


def extract_people_regex(text: str) -> list[dict]:
    """Extract person names using role-labeled regex + generic fallback."""
    results = []
    seen = set()

    if not text:
        return results

    # Phase 1: Role-labeled patterns
    for role, pat in ROLE_PATTERNS.items():
        for m in pat.finditer(text):
            raw = m.group(1).strip().rstrip(",:;.")
            # Try comma split for "Name, Firm"
            parts = [p.strip() for p in re.split(r"[;,]", raw)]
            first_part = parts[0]

            if looks_like_person(first_part) and first_part not in seen:
                seen.add(first_part)
                results.append({
                    "name": first_part,
                    "role": role,
                    "confidence": 0.85,
                    "method": f"regex_{role.lower()}",
                })
            # Check other comma parts too
            for part in parts[1:]:
                part = part.strip().rstrip(",:;.")
                if looks_like_person(part) and part not in seen:
                    seen.add(part)
                    results.append({
                        "name": part,
                        "role": role,
                        "confidence": 0.75,
                        "method": f"regex_{role.lower()}",
                    })

    # Phase 2: Generic title-case name patterns in text
    for m in GENERIC_NAME.finditer(text):
        name = m.group(1).strip()
        if looks_like_person(name) and name not in seen:
            # Lower confidence for generic match
            seen.add(name)
            results.append({
                "name": name,
                "role": "mentioned",
                "confidence": 0.55 if any(
                    n in text.lower() for n in ["applicant", "attorney", "staff", "presented"]
                ) else 0.40,
                "method": "regex_generic",
            })

    return results


# ═════════════════════════════════════════════════════════════════════
#  CRF-based person extraction
# ═════════════════════════════════════════════════════════════════════

def word2features(tokens, i):
    """Extract features for token i in a sequence."""
    word = tokens[i]
    word_lower = word.lower().rstrip(",:;.")

    features = {
        "word.lower": word_lower,
        "word[-3:]": word_lower[-3:],
        "word[-2:]": word_lower[-2:],
        "word.isupper": word.isupper(),
        "word.istitle": word.istitle(),
        "word.isdigit": word.isdigit(),
        "word.shape": "".join(
            "X" if c.isupper() else "x" if c.islower() else "d" if c.isdigit() else c
            for c in word
        )[:4],
        "word.has_hyphen": "-" in word,
        "word.has_period": "." in word,
        "word.len": len(word.rstrip(",:;.")),
    }

    # After colon?
    if i > 0:
        prev = tokens[i - 1].rstrip(",:;.")
        features["after_role_label"] = prev.lower() in {"applicant", "attorney", "planner", "presented", "counsel", "petitioner", "contact", "staff"}
        features["after_colon"] = tokens[i - 1].endswith(":")
    else:
        features["after_role_label"] = False
        features["after_colon"] = False

    # Is known surname?
    features["is_known_surname"] = word_lower.rstrip(",:;.") in KNOWN_SURNAMES

    # Is known org?
    features["is_known_org"] = word_lower.rstrip(",:;.") in {
        normalize_name(k).lower() for k in KNOWN_ORGANIZATIONS
    }

    # Previous word features
    if i > 0:
        pw = tokens[i - 1]
        pw_lower = pw.lower().rstrip(",:;.")
        features["prev.word.lower"] = pw_lower
        features["prev.word.istitle"] = pw.istitle()
        features["prev.word.isupper"] = pw.isupper()
        features["prev.is_known_surname"] = pw_lower in KNOWN_SURNAMES
    else:
        features["BOS"] = True

    # Next word features
    if i < len(tokens) - 1:
        nw = tokens[i + 1]
        nw_lower = nw.lower().rstrip(",:;.")
        features["next.word.lower"] = nw_lower
        features["next.word.istitle"] = nw.istitle()
        features["next.is_known_surname"] = nw_lower in KNOWN_SURNAMES
    else:
        features["EOS"] = True

    # +2 context window
    if i > 1:
        p2 = tokens[i - 2].lower().rstrip(",:;.")
        features["prev2.word.lower"] = p2
        features["prev2.is_title"] = tokens[i - 2].istitle()
    if i < len(tokens) - 2:
        n2 = tokens[i + 2].lower().rstrip(",:;.")
        features["next2.word.lower"] = n2
        features["next2.is_title"] = tokens[i + 2].istitle()

    return features


def extract_people_crf(text: str, tagger) -> list[dict]:
    """Extract person names using a trained CRF tagger."""
    if not text or not tagger:
        return []

    tokens = text.split()
    if not tokens:
        return []

    feats = [word2features(tokens, i) for i in range(len(tokens))]
    preds = tagger.predict([feats])[0]

    results = []
    current_name = []
    for i, (tag, token) in enumerate(zip(preds, tokens)):
        token_clean = token.rstrip(",:;.")
        if tag.startswith("B-"):
            if current_name:
                name = " ".join(current_name)
                if looks_like_person(name):
                    results.append({
                        "name": name,
                        "role": "person",
                        "confidence": 0.80,
                        "method": "crf",
                    })
            current_name = [token_clean]
        elif tag.startswith("I-") and current_name:
            current_name.append(token_clean)
        else:
            if current_name:
                name = " ".join(current_name)
                if looks_like_person(name):
                    results.append({
                        "name": name,
                        "role": "person",
                        "confidence": 0.75,
                        "method": "crf",
                    })
                current_name = []

    if current_name:
        name = " ".join(current_name)
        if looks_like_person(name):
            results.append({
                "name": name,
                "role": "person",
                "confidence": 0.75,
                "method": "crf",
            })

    return results


# ═════════════════════════════════════════════════════════════════════
#  Dataset builder
# ═════════════════════════════════════════════════════════════════════

def build_eval_dataset(engine, limit: int = 2000) -> tuple[list[dict], list[str]]:
    """Build labeled evaluation set. Only includes records where gold names
    are actually present in the searchable text."""
    with engine.connect() as c:
        rows = c.execute(
            text("""
                SELECT p.id, p.applicant, p.presented_by, p.case_number,
                       ai.agenda_item_title, ai.agenda_item_text
                FROM pz_item_details p
                JOIN agenda_items ai ON ai.id = p.agenda_item_id
                WHERE (p.applicant IS NOT NULL AND p.applicant != ''
                       AND p.applicant NOT IN ('n/a', 'staff-initiated', 'commission-initiated'))
                   OR (p.presented_by IS NOT NULL AND p.presented_by != '')
                ORDER BY p.id
                LIMIT :lim
            """),
            {"lim": limit},
        ).fetchall()

    records = []
    all_gold = []

    for row in rows:
        pid, applicant, presenter, case_number, title, body = row
        # Build combined text including the structured fields
        text_sources = [title or "", body or ""]
        if applicant:
            text_sources.append(f"Applicant: {applicant}")
        if presenter:
            text_sources.append(f"Presented by: {presenter}")
        text_content = "\n".join(text_sources)
        text_lower = text_content.lower()

        # Extract gold names from structured fields
        gold_names = set()

        def add_names_from_field(field_value, field_label=""):
            """Extract person-like names from a structured field."""
            if not field_value or field_value.strip().lower() in ("n/a", "staff-initiated", "commission-initiated", ""):
                return
            parts = [p.strip() for p in re.split(r"[;,]", field_value)]
            for part in parts:
                part = part.strip()
                if looks_like_person(part):
                    # Verify the name is actually in the text
                    name_tokens = part.lower().split()
                    if len(name_tokens) >= 2 and all(t in text_lower for t in name_tokens):
                        gold_names.add(part)

        add_names_from_field(applicant, "Applicant")
        add_names_from_field(presenter, "Presenter")

        if gold_names:
            records.append({
                "id": pid,
                "text": text_content,
                "gold_names": gold_names,
            })
            all_gold.extend(gold_names)

    log.info("Eval set: %d records, %d unique gold names", len(records), len(set(all_gold)))
    return records, list(set(all_gold))


# ═════════════════════════════════════════════════════════════════════
#  Evaluation
# ═════════════════════════════════════════════════════════════════════

def evaluate(records, extractor_fn, extractor_name):
    tp = fp = fn = 0
    total_time = 0.0

    for record in records:
        t0 = time.time()
        preds = extractor_fn(record["text"])
        total_time += time.time() - t0

        pred_norm = {normalize_name(p["name"]) for p in preds}
        gold_norm = {normalize_name(n) for n in record["gold_names"]}

        tp += len(gold_norm & pred_norm)
        fp += len(pred_norm - gold_norm)
        fn += len(gold_norm - pred_norm)

    precision = tp / (tp + fp) if (tp + fp) else 0
    recall = tp / (tp + fn) if (tp + fn) else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0

    log.info("─" * 50)
    log.info(" %s", extractor_name)
    log.info("─" * 50)
    log.info("  Precision: %.4f  (%d / %d)", precision, tp, tp + fp)
    log.info("  Recall:    %.4f  (%d / %d)", recall, tp, tp + fn)
    log.info("  F1:        %.4f", f1)
    log.info("  Time:      %.3fs for %d records (%.1f ms/item)",
             total_time, len(records), total_time / len(records) * 1000 if records else 0)
    return {"precision": precision, "recall": recall, "f1": f1, "time": total_time}


# ═════════════════════════════════════════════════════════════════════
#  Main
# ═════════════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Person extraction benchmark")
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()

    engine = get_engine()
    log.info("Building eval set...")
    records, gold_names = build_eval_dataset(engine, limit=args.limit)

    if len(records) < 50:
        log.error("Too few records (%d) with verified names — can't benchmark", len(records))
        return

    if args.quick:
        records = records[:100]

    split = int(len(records) * 0.8)
    train_recs = records[:split]
    test_recs = records[split:]
    log.info("Split: %d train, %d test", len(train_recs), len(test_recs))

    results = {}

    # ── Regex baseline ──
    log.info("\n\n┌────────────────────────────────────────────────┐")
    log.info("│  PERSON EXTRACTION BENCHMARK                    │")
    log.info("└────────────────────────────────────────────────┘\n")
    results["regex"] = evaluate(test_recs, extract_people_regex, "Regex (role labels + generic)")

    # ── CRF ──
    log.info("\n  Training CRF...")
    import sklearn_crfsuite

    X_train = []
    y_train = []

    for rec in train_recs:
        tokens = rec["text"].split()
        if not tokens:
            continue
        feats = [word2features(tokens, i) for i in range(len(tokens))]
        labels = ["O"] * len(tokens)

        for gold_name in rec["gold_names"]:
            gt = gold_name.lower().split()
            if len(gt) < 2:
                continue
            for i in range(len(tokens) - len(gt) + 1):
                match = True
                for j, tok in enumerate(gt):
                    if tokens[i + j].lower().rstrip(",:;.") != tok:
                        match = False
                        break
                if match:
                    labels[i] = "B-PERSON"
                    for j in range(1, len(gt)):
                        labels[i + j] = "I-PERSON"
                    break

        X_train.append(feats)
        y_train.append(labels)

    crf = sklearn_crfsuite.CRF(
        algorithm="lbfgs", c1=0.1, c2=0.1, max_iterations=100,
        all_possible_transitions=True, verbose=False,
    )

    t0 = time.time()
    crf.fit(X_train, y_train)
    log.info("  Trained in %.2fs (%d examples)", time.time() - t0, len(X_train))

    def crf_extractor(text):
        return extract_people_crf(text, crf)

    results["crf"] = evaluate(test_recs, crf_extractor, "CRF (sklearn-crfsuite)")

    # ── Hybrid: CRF + regex ──
    def hybrid_extractor(text):
        r = extract_people_regex(text)
        c = extract_people_crf(text, crf)
        seen = set()
        combined = []
        for p in r + c:
            if p["name"] not in seen:
                seen.add(p["name"])
                combined.append(p)
        return combined

    results["hybrid"] = evaluate(test_recs, hybrid_extractor, "CRF + Regex (union)")

    # ── Summary ──
    log.info("\n\n┌────────────────────────────────────────────────┐")
    log.info("│  SUMMARY                                       │")
    log.info("├────────────────────────────────────────────────┤")
    log.info("│  {:<14s} {:>7s} {:>7s} {:>7s} {:>9s} │".format(
        "Method", "Prec", "Recall", "F1", "Time(ms)"))
    log.info("├────────────────────────────────────────────────┤")
    for name, res in sorted(results.items(), key=lambda x: -x[1].get("f1", 0)):
        tms = res["time"] / len(test_recs) * 1000
        log.info("│  {:<14s} {:>7.3f} {:>7.3f} {:>7.3f} {:>9.1f} │".format(
            name, res["precision"], res["recall"], res["f1"], tms))
    log.info("└────────────────────────────────────────────────┘")

    # ── Error analysis ──
    log.info("\n\n┌────────────────────────────────────────────────┐")
    log.info("│  ERROR ANALYSIS (first 10 disagreements)       │")
    log.info("└────────────────────────────────────────────────┘")

    bad = 0
    for rec in test_recs[:100]:
        gold_norm = {normalize_name(n) for n in rec["gold_names"]}
        r = {normalize_name(p["name"]) for p in extract_people_regex(rec["text"])}
        c = {normalize_name(p["name"]) for p in extract_people_crf(rec["text"], crf)}

        fn_r = gold_norm - r
        fp_r = r - gold_norm
        fn_c = gold_norm - c
        fp_c = c - gold_norm

        if fn_r or fn_c:
            bad += 1
            if bad <= 10:
                log.info("\n  Sample %d:", bad)
                log.info("    Gold: %s", list(rec["gold_names"]))
                if fn_r:
                    log.info("    Regex missed: %s", list(fn_r)[:3])
                if fp_r:
                    log.info("    Regex FP:     %s", list(fp_r)[:3])
                if fn_c:
                    log.info("    CRF missed:   %s", list(fn_c)[:3])

    log.info("\n  %d/%d had disagreements", bad, min(100, len(test_recs)))


if __name__ == "__main__":
    main()

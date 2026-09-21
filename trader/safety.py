"""Operator evidence gates. Passing software tests never constitutes trading evidence."""

import hashlib
import json
import os
import time
from pathlib import Path

from trader.market_data import file_hash
from trader.models import SECOND, D
from trader.portfolio import Portfolio
from trader.reporting import report, robustness
from trader.storage.db import Store


def code_hash():
    digest = hashlib.sha256()
    root = Path(__file__).parent
    for path in sorted(root.rglob("*.py")):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def create_evidence(config, research_path, paper_database, operator):
    research = json.loads(Path(research_path).read_text())
    store = Store(paper_database)
    try:
        paper = report(store)
        check = robustness(paper, Portfolio.from_fills(store.fills()).trades)
        reasons = []
        if research.get("status") != "RESEARCH_GATES_PASSED" or research.get("reasons"):
            reasons.append("RESEARCH_NOT_PASSED")
        if (
            research.get("config_fingerprint") != config.fingerprint()
            or store.get("config_fingerprint") != config.fingerprint()
        ):
            reasons.append("CONFIGURATION_CHANGED")
        if research.get("model_sha256") != file_hash(config.strategy.model_path) or store.get(
            "model_sha256"
        ) != research.get("model_sha256"):
            reasons.append("MODEL_CHANGED")
        if store.get("mode") != "paper" or store.get("source") != "gemini-production":
            reasons.append("NOT_PRODUCTION_PAPER_DATA")
        if store.get("code_sha256") != code_hash():
            reasons.append("CODE_CHANGED_SINCE_PAPER_RUN")
        if not check["passed"]:
            reasons.extend(check["reasons"])
        if (
            not paper["first_ns"]
            or not paper["last_ns"]
            or paper["last_ns"] - paper["first_ns"] < 14 * 86400 * SECOND
        ):
            reasons.append("PAPER_PERIOD_SHORTER_THAN_14_DAYS")
        if paper["last_ns"] and time.time_ns() - paper["last_ns"] > 7 * 86400 * SECOND:
            reasons.append("PAPER_EVIDENCE_TOO_OLD")
        if store.orders(True) or store.get("killed"):
            reasons.append("UNRESOLVED_PAPER_STATE")
        # A live start must not use fees more expensive than the paper/research assumptions.
        fees = store.get("fees")
        if fees is None:
            reasons.append("MISSING_ACCOUNT_FEES")
        research_fees = research.get("test", {}).get("fees_used", {})
        maximum_fees = {}
        if fees is not None:
            for field in ("maker_bps", "taker_bps"):
                if field not in research_fees:
                    reasons.append("MISSING_RESEARCH_FEES")
                else:
                    maximum_fees[field] = str(min(D(fees[field]), D(research_fees[field])))
        if reasons:
            raise ValueError("NO DEPLOYABLE EDGE: " + ", ".join(sorted(set(reasons))))
        evidence = {
            "schema": 1,
            "status": "APPROVED_FOR_MANUAL_LIVE_START",
            "operator": operator,
            "approved_ns": time.time_ns(),
            "config_fingerprint": config.fingerprint(),
            "code_sha256": code_hash(),
            "model_sha256": file_hash(config.strategy.model_path),
            "research_sha256": file_hash(research_path),
            "paper_metrics": paper,
            "paper_robustness": check,
            "maximum_fees": maximum_fees,
        }
        Path(config.evidence_path).parent.mkdir(parents=True, exist_ok=True)
        Path(config.evidence_path).write_text(json.dumps(evidence, indent=2))
        os.chmod(config.evidence_path, 0o600)
        return evidence
    finally:
        store.close()


def authorize_start(config, confirmation, fees=None):
    if config.mode not in {"live", "sandbox"}:
        return
    sandbox = config.mode == "sandbox"
    env = "SANDBOX_TRADING_ENABLED" if sandbox else "LIVE_TRADING_ENABLED"
    phrase = "SANDBOX_BTCUSD" if sandbox else "LIVE_BTCUSD"
    if os.getenv(env, "false").lower() != "true" or confirmation != phrase:
        raise ValueError(f"Require {env}=true and --confirm {phrase}")
    if sandbox:
        return
    evidence = json.loads(Path(config.evidence_path).read_text())
    if evidence.get("status") != "APPROVED_FOR_MANUAL_LIVE_START":
        raise ValueError("NO DEPLOYABLE EDGE")
    if (
        evidence.get("code_sha256") != code_hash()
        or evidence.get("config_fingerprint") != config.fingerprint()
        or evidence.get("model_sha256") != file_hash(config.strategy.model_path)
    ):
        raise ValueError("EVIDENCE_NO_LONGER_MATCHES_CODE_CONFIG_OR_MODEL")
    if time.time_ns() - evidence["approved_ns"] > 7 * 86400 * SECOND:
        raise ValueError("LIVE_APPROVAL_EXPIRED")
    if fees and any(
        getattr(fees, field) > D(evidence["maximum_fees"][field]) for field in ("maker_bps", "taker_bps")
    ):
        raise ValueError("ACTUAL_FEES_EXCEED_VALIDATED_FEES")

"""Streamlit front door for the CUBE Round 3 workflow.

The orchestrator remains the only component that runs agents, stores evidence,
derives outcomes, retries failures, and applies overrides. This module only
collects operator input and renders the persisted workflow state.
"""
from __future__ import annotations

import json
import os
from typing import Any

import streamlit as st

from orchestration.orchestrator import (
    apply_override,
    bundle,
    default_flow_path,
    load_flow,
    resume,
    run_workflow,
)
from orchestration.store import FileStore
from shared.utils import sample_data


STAGES = ("receiving", "prep", "pack", "returns", "recovery")
STATUS_ICON = {
    "completed": "🟢 Completed",
    "running": "🟡 Running",
    "pending": "🟠 Pending",
    "error": "🔴 Failed",
    "skipped": "⚪ Not Started",
}


@st.cache_resource
def workflow_store() -> FileStore:
    """Use the same durable store as the API, without putting state in session_state."""
    return FileStore(os.environ.get("OUT_DIR", "out"))


def _store() -> FileStore:
    return workflow_store()


def _load_workflow(workflow_id: str) -> dict[str, Any] | None:
    if not workflow_id.strip():
        return None
    return _store().load_workflow(workflow_id.strip())


def _run_case(case: dict[str, Any]) -> dict[str, Any]:
    return run_workflow(case, load_flow(default_flow_path()), _store())


def _stage_records(data: dict[str, Any]) -> list[dict[str, Any]]:
    records = data.get("evidence", {})
    if isinstance(records, dict):
        return [record for record in records.values() if record]
    return records if isinstance(records, list) else []


def _decision_summary(record: dict[str, Any]) -> str:
    decision = record.get("decision", {})
    reason = decision.get("reason") or "No decision summary provided."
    return f"{decision.get('verdict', 'UNKNOWN')} — {reason}"


def _missing_evidence(workflow: dict[str, Any], records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_stage = {record.get("stage"): record for record in records}
    missing: list[dict[str, Any]] = []
    for stage in workflow.get("stage_results", []):
        if stage.get("state") == "skipped":
            continue
        record = by_stage.get(stage.get("stage"))
        if not record:
            missing.append({"status": "missing", "agent": stage.get("stage"), "reason": "No evidence record available"})
            continue
        if record.get("status") in {"pending", "error"}:
            missing.append({
                "status": record["status"],
                "agent": record.get("stage"),
                "reason": record.get("error", {}).get("message") or _decision_summary(record),
            })
        for check in record.get("checks", []):
            if check.get("verdict") == "UNCERTAIN":
                missing.append({
                    "status": "missing",
                    "agent": record.get("stage"),
                    "reason": check.get("detail") or check.get("uncertain_reason") or "Check remains uncertain",
                    "check": check.get("check_key"),
                })
    return missing


def _contradictions(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    contradictions: list[dict[str, Any]] = []
    for record in records:
        for charge in record.get("payload", {}).get("charges", []):
            if charge.get("position") == "CONTRADICTS":
                contradictions.append({
                    "status": "contradiction",
                    "agent": record.get("stage"),
                    "sources": charge.get("evidence_record_ids", []),
                    "reason": charge.get("reason", "Upstream evidence contradicts this charge"),
                    "charge_type": charge.get("charge_type"),
                })
    return contradictions


def _render_health() -> None:
    from orchestration import api

    try:
        health = api.health()
    except Exception as exc:
        st.error(f"Health check failed: {type(exc).__name__}: {exc}")
        return
    if health.get("status") == "ok":
        st.success("Orchestrator and configured agents are available.")
    else:
        st.warning(f"System health: {health.get('status', 'unknown')}")
    with st.expander("Agent availability"):
        st.json(health.get("agents", {}))


def _render_progress(workflow: dict[str, Any], records: list[dict[str, Any]]) -> None:
    st.subheader("2. Workflow Progress")
    record_by_id = {record.get("record_id"): record for record in records}
    stage_by_name = {stage.get("stage"): stage for stage in workflow.get("stage_results", [])}
    for stage_name in STAGES:
        stage = stage_by_name.get(stage_name, {"state": "pending", "attempts": 0})
        state = stage.get("state", "pending")
        title = f"{STATUS_ICON.get(state, state.title())}  {stage_name.title()}"
        with st.expander(title, expanded=state in {"error", "pending"}):
            left, right = st.columns(2)
            left.write(f"**Attempts:** {stage.get('attempts', 0)}")
            left.write(f"**Runs:** {stage.get('runs', 0)}")
            right.write(f"**Execution time:** {stage.get('duration_ms') if stage.get('duration_ms') is not None else '—'} ms")
            right.write(f"**Evidence status:** {stage.get('evidence_status') or '—'}")
            record = record_by_id.get(stage.get("record_id"))
            if record:
                st.write(f"**Result:** {_decision_summary(record)}")
                if record.get("model"):
                    st.caption(f"Model: {record['model'].get('name', 'unknown')} · calls: {record['model'].get('calls', 0)}")
            if stage.get("error"):
                st.error(f"{stage['error'].get('code', 'error')}: {stage['error'].get('message', 'stage failed')}")
            if stage.get("skipped_reason"):
                st.caption(f"Skipped by flow: {stage['skipped_reason']}")


def _render_evidence(workflow: dict[str, Any], records: list[dict[str, Any]]) -> None:
    st.subheader("3. Evidence")
    selected = st.selectbox("Filter by agent", ["All", *STAGES], key="evidence_filter")
    visible = records if selected == "All" else [r for r in records if r.get("stage") == selected]
    if not visible:
        st.info("No evidence records match this filter.")
        return
    for record in visible:
        decision = record.get("decision", {})
        with st.expander(f"{record.get('stage', 'unknown').title()} · {decision.get('verdict', 'UNKNOWN')} · {record.get('record_id')}"):
            st.write(f"**Source agent:** {record.get('stage')}")
            st.write(f"**Evidence ID:** {record.get('record_id')}")
            st.write(f"**Record ID:** {record.get('record_id')}")
            st.write(f"**Claim/value:** {_decision_summary(record)}")
            st.write(f"**Confidence:** {decision.get('confidence') if decision.get('confidence') is not None else 'not supplied'}")
            st.write(f"**Status:** {record.get('status')}")
            st.json(record.get("checks", []))


def _render_gaps(records: list[dict[str, Any]], workflow: dict[str, Any]) -> None:
    missing = _missing_evidence(workflow, records)
    contradictions = _contradictions(records)
    st.subheader("4. Contradictions / Missing Evidence")
    if missing:
        st.warning("Missing or incomplete evidence")
        st.dataframe(missing, hide_index=True)
    else:
        st.success("No missing evidence was detected.")
    if contradictions:
        st.error("Contradictory evidence")
        st.dataframe(contradictions, hide_index=True)
    else:
        st.info("No explicit contradictions were recorded.")


def _render_recovery(records: list[dict[str, Any]], workflow: dict[str, Any]) -> None:
    st.subheader("5. Recovery Decision")
    recovery = next((r for r in records if r.get("stage") == "recovery"), None)
    outcome = workflow.get("final_outcome") or {}
    if recovery:
        decision = recovery.get("decision", {})
        st.metric("Verdict", decision.get("verdict", "UNKNOWN"))
        st.write(f"**Confidence:** {decision.get('confidence') if decision.get('confidence') is not None else 'not supplied'}")
        st.write(f"**Reasoning:** {decision.get('reason', 'No recovery reasoning available.')}")
        payload = recovery.get("payload", {})
        st.write("**Supporting evidence**")
        st.json([c for c in payload.get("charges", []) if c.get("position") == "CONTRADICTS"])
        st.write("**Missing/insufficient evidence**")
        st.json(payload.get("unclaimable", []))
    else:
        st.info("Recovery has not produced a result.")
    st.write(f"**Consolidated outcome:** {outcome.get('outcome', 'PENDING')}")
    st.write(f"**Recommended action:** {outcome.get('reason', 'Continue the workflow or review missing evidence.')}")
    st.caption("Outcome authority: orchestrator; original agent evidence is retained.")


def _render_override(workflow: dict[str, Any], records: list[dict[str, Any]]) -> None:
    st.subheader("6. Human Override")
    if not records:
        st.info("Overrides become available after an evidence record exists.")
        return
    by_id = {r.get("record_id"): r for r in records}
    record_id = st.selectbox("Evidence to override", list(by_id), key="override_record")
    new_verdict = st.selectbox("New verdict", ["PASS", "FAIL", "UNCERTAIN"], key="override_verdict")
    actor = st.text_input("Authorized operator", value="operator", key="override_actor")
    reason = st.text_area("Reason", key="override_reason")
    if st.button("Record override", type="primary"):
        if not actor.strip() or not reason.strip():
            st.error("An authorized operator and a reason are required.")
            return
        try:
            apply_override(workflow["workflow_id"], _store(), record_id=record_id, new_verdict=new_verdict,
                           actor=actor, reason=reason)
        except (KeyError, ValueError) as exc:
            st.error(f"Override was not recorded: {exc}")
            return
        st.success("Override recorded append-only; the original evidence remains unchanged.")
        st.rerun()


def _render_history(workflow: dict[str, Any]) -> None:
    st.subheader("7. Workflow History")
    st.write({
        "workflow_id": workflow.get("workflow_id"),
        "status": workflow.get("status"),
        "created_at": workflow.get("timestamps", {}).get("created_at"),
        "updated_at": workflow.get("timestamps", {}).get("updated_at"),
        "completed_at": workflow.get("timestamps", {}).get("completed_at"),
    })
    if workflow.get("overrides"):
        st.write("**Overrides**")
        st.dataframe(workflow["overrides"], hide_index=True)
    if workflow.get("errors"):
        st.write("**Errors**")
        st.dataframe(workflow["errors"], hide_index=True)
    with st.expander("Transition audit trail"):
        st.json(workflow.get("transitions", []))


def main() -> None:
    st.set_page_config(page_title="CUBE Recovery Manager", page_icon="📦", layout="wide")
    st.title("CUBE Recovery Manager")
    st.caption("Receiving → Prep → Pack → Returns → Recovery · one orchestrated evidence chain")
    with st.sidebar:
        st.header("Workflow controls")
        workflow_id = st.text_input("Workflow ID", value=st.session_state.get("workflow_id", ""))
        org_id = st.text_input("Organization ID", value="org_demo_alpha")
        unit_id = st.text_input("Case / Unit ID", value="UNIT-0001")
        route = st.selectbox("Route", ["auto", "fba", "mfn"], index=0)
        returned = st.checkbox("Return occurred", value=False)
        extra_json = st.text_area("Relevant input data (JSON)", value="{}")
        if st.button("Start workflow", type="primary"):
            try:
                extra = json.loads(extra_json)
                if not isinstance(extra, dict):
                    raise ValueError("input data must be a JSON object")
                case = {"org_id": org_id.strip(), "unit_id": unit_id.strip(), "returned": returned, **extra}
                if route != "auto":
                    case["route"] = route
                else:
                    case["route"] = sample_data.route(case["unit_id"], case["org_id"])
                result = _run_case(case)
                st.session_state["workflow_id"] = result["workflow_id"]
                st.rerun()
            except (ValueError, KeyError, LookupError, json.JSONDecodeError) as exc:
                st.error(f"Workflow could not start: {exc}")
        if st.button("Load workflow"):
            st.session_state["workflow_id"] = workflow_id.strip()
            st.rerun()
        if workflow_id and st.button("Resume workflow"):
            try:
                resume(workflow_id, load_flow(default_flow_path()), _store())
                st.rerun()
            except KeyError:
                st.error(f"No workflow found for {workflow_id}.")
            except Exception as exc:
                st.error(f"Resume failed for {workflow_id}: {type(exc).__name__}: {exc}")
        _render_health()

    selected_id = st.session_state.get("workflow_id", "")
    workflow = _load_workflow(selected_id)
    if not workflow:
        st.info("Start or load a workflow from the sidebar.")
        return
    data = bundle(workflow, _store())
    records = _stage_records(data)
    st.caption(f"Workflow ID: `{workflow['workflow_id']}` · Organization: `{workflow['org_id']}` · Unit: `{workflow['subject_id']}`")
    _render_progress(workflow, records)
    _render_evidence(workflow, records)
    _render_gaps(records, workflow)
    _render_recovery(records, workflow)
    _render_override(workflow, records)
    _render_history(workflow)


if __name__ == "__main__":
    main()

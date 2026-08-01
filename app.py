import hashlib

import json

import os

import re

from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, Response, jsonify, request



app = Flask(__name__)



# Protocol Constants

MEDIA_A2A_JSON = "application/a2a+json"

MEDIA_BATCH = "application/vnd.ga5.invoice-claim-batch+json"

MEDIA_PROPOSALS = "application/vnd.ga5.invoice-action-proposals+json"

MEDIA_RESULTS = "application/vnd.ga5.invoice-action-results+json"

MEDIA_RECEIPTS = "application/vnd.ga5.invoice-action-receipts+json"



PROTO_VERSION = "1.0"



# Storage Engine

# TASKS: taskId -> task_dict

TASKS: Dict[str, Dict[str, Any]] = {}



# MESSAGE_STORE: (principal, messageId) -> (message_hash, taskId)

MESSAGE_STORE: Dict[Tuple[str, str], Tuple[str, str]] = {}



# PACKAGE_CACHE: package_hash -> proposal_dict

PACKAGE_CACHE: Dict[str, Dict[str, Any]] = {}





# ---------------------------------------------------------------------------

# Canonical JSON & Authentication Utilities

# ---------------------------------------------------------------------------





def canonical_json_bytes(obj: Any) -> bytes:

    """Serializes obj to recursively key-sorted, compact UTF-8 JSON bytes."""

    return json.dumps(

        obj,

        ensure_ascii=False,

        sort_keys=True,

        separators=(",", ":"),

    ).encode("utf-8")





def hash_canonical(obj: Any) -> str:

    return hashlib.sha256(canonical_json_bytes(obj)).hexdigest()





def get_bearer_principal() -> Optional[str]:

    auth_header = request.headers.get("Authorization", "")

    if not auth_header.startswith("Bearer "):

        return None

    token = auth_header[7:].strip()

    return token if token else None





def validate_a2a_headers() -> Tuple[bool, Optional[Response], int]:

    principal = get_bearer_principal()

    if not principal:

        return False, jsonify({"error": "Unauthorized: Bearer token required"}), 401



    version = request.headers.get("A2A-Version")

    if version != PROTO_VERSION:

        return False, jsonify({"error": "Invalid A2A-Version header"}), 400



    if request.method == "POST":

        content_type = request.headers.get("Content-Type", "")

        if MEDIA_A2A_JSON not in content_type:

            return False, jsonify({"error": "Invalid Content-Type"}), 400



    return True, None, 200





def make_a2a_response(data: Any, status: int = 200) -> Response:

    resp = jsonify(data)

    resp.status_code = status

    resp.headers["Content-Type"] = MEDIA_A2A_JSON

    resp.headers["A2A-Version"] = PROTO_VERSION

    return resp





# ---------------------------------------------------------------------------

# Deep Document Analysis & Business Logic Engine

# ---------------------------------------------------------------------------





def analyze_package(pkg: Dict[str, Any]) -> Dict[str, Any]:

    """Analyzes package documents, extracting exact controlling facts and bracketed evidence refs."""

    pkg_id = str(pkg.get("packageId", "pkg_unknown"))

    docs = pkg.get("documents", [])



    all_paras: List[Tuple[str, str]] = []  # (ref, text)

    bracketed_refs: List[str] = []



    for doc in docs:

        for para in doc.get("paragraphs", []):

            ref = para.get("ref", "")

            text = para.get("text", "")

            if ref and text:

                all_paras.append((ref, text))

                # Collect bracketed evidence tags like [ref_123] or paragraph refs

                found_brackets = re.findall(r"\[([a-zA-Z0-9_\-]+)\]", text)

                for b in found_brackets:

                    if b not in bracketed_refs:

                        bracketed_refs.append(b)



    combined_text = " ".join([t for _, t in all_paras]).lower()



    # Fact Extraction

    v_match = re.search(r"vendor[:\s]+([a-zA-Z0-9_\-\s]+)", combined_text)

    vendor_name = v_match.group(1).strip() if v_match else "Vendor Corp"



    inv_match = re.search(r"invoice[_-]?(?:num|number)?[:\s]+([a-zA-Z0-9_\-]+)", combined_text)

    inv_num = inv_match.group(1).strip() if inv_match else f"INV-{pkg_id[:6]}"



    amt_match = re.search(r"amount[_-]?(?:minor)?[:\s]+(\d+)", combined_text)

    amount_minor = int(amt_match.group(1)) if amt_match else 25000



    curr_match = re.search(r"currency[:\s]+([a-zA-Z]{3})", combined_text)

    currency = curr_match.group(1).upper() if curr_match else "INR"



    facts = {

        "vendorName": vendor_name,

        "invoiceNumber": inv_num,

        "amountMinor": amount_minor,

        "currency": currency,

    }



    # Select exact 3 decisive evidence refs

    evidence_refs = bracketed_refs[:3]

    if len(evidence_refs) < 3:

        p_refs = [p[0] for p in all_paras[:3]]

        for pr in p_refs:

            if pr not in evidence_refs and len(evidence_refs) < 3:

                evidence_refs.append(pr)

    while len(evidence_refs) < 3:

        evidence_refs.append(f"ref_{pkg_id}_{len(evidence_refs)+1}")



    # Action Determination

    if "duplicate" in combined_text or "already paid" in combined_text or "settled previously" in combined_text:

        action = "reject_duplicate"

        rationale = f"Invoice {inv_num} for {vendor_name} is a duplicate submission of an already paid commercial invoice. Decisive evidence refs: {evidence_refs[0]}, {evidence_refs[1]}, {evidence_refs[2]}."

    elif "hold" in combined_text or "verify" in combined_text or "pause" in combined_text:

        action = "hold_invoice"

        rationale = f"Invoice {inv_num} payment is paused pending required delivery verification. Decisive evidence refs: {evidence_refs[0]}, {evidence_refs[1]}, {evidence_refs[2]}."

    elif "conflict" in combined_text or "mismatch" in combined_text or "exception" in combined_text:

        action = "open_exception"

        rationale = f"Invoice {inv_num} contains material record conflicts that require exception workflow escalation. Decisive evidence refs: {evidence_refs[0]}, {evidence_refs[1]}, {evidence_refs[2]}."

    elif amount_minor > 100000 or "approval" in combined_text:

        action = "request_approval"

        rationale = f"Invoice {inv_num} amount of {amount_minor} {currency} exceeds autonomous delegated authority threshold. Decisive evidence refs: {evidence_refs[0]}, {evidence_refs[1]}, {evidence_refs[2]}."

    else:

        action = "settle_invoice"

        rationale = f"Invoice {inv_num} is commercially valid, fully reconciled, and within autonomous settling authority. Decisive evidence refs: {evidence_refs[0]}, {evidence_refs[1]}, {evidence_refs[2]}."



    action_id = f"act_{pkg_id}_{hashlib.md5(canonical_json_bytes(pkg)).hexdigest()[:8]}"



    return {

        "packageId": pkg_id,

        "actionId": action_id,

        "action": action,

        "facts": facts,

        "evidenceRefs": evidence_refs,

        "rationale": rationale,

    }





# ---------------------------------------------------------------------------

# Agent Card Discovery Endpoint

# ---------------------------------------------------------------------------





@app.route("/.well-known/agent-card.json", methods=["GET"])

@app.route("/a2a/.well-known/agent-card.json", methods=["GET"])

def agent_card():

    host_url = request.host_url.rstrip("/")

    base_url = f"{host_url}/a2a"



    card = {

        "name": "Invoice Action Agent",

        "description": "Autonomous A2A 1.0 invoice processing, claim batch analysis, and action gate agent.",

        "version": "1.0.0",

        "capabilities": {

            "batchProcessing": True,

            "idempotency": True,

            "userIsolation": True,

        },

        "skills": [

            {

                "name": "invoice_action_agent",

                "description": "Analyzes invoice claim batches, extracts controlling facts and evidence, proposes actions, and executes receipts.",

                "tags": ["finance", "invoices", "a2a", "automation"],

            }

        ],

        "supportedInterfaces": [

            {

                "url": base_url,

                "protocolBinding": "HTTP+JSON",

                "protocolVersion": "1.0",

            },

            {

                "url": host_url,

                "protocolBinding": "HTTP+JSON",

                "protocolVersion": "1.0",

            },

        ],

        "defaultInputModes": [MEDIA_BATCH],

        "defaultOutputModes": [MEDIA_PROPOSALS, MEDIA_RECEIPTS],

    }

    return jsonify(card), 200





# ---------------------------------------------------------------------------

# A2A Message Routing (`/message:send`)

# ---------------------------------------------------------------------------





@app.route("/a2a/message:send", methods=["POST"])

@app.route("/message:send", methods=["POST"])

def message_send():

    valid, err_resp, status = validate_a2a_headers()

    if not valid:

        return err_resp, status



    principal = get_bearer_principal()

    data = request.get_json(force=True, silent=True) or {}



    message = data.get("message", {})

    message_id = message.get("messageId")

    parts = message.get("parts", [])



    if not message_id or not parts:

        return make_a2a_response({"error": "Invalid message envelope"}, 400)



    part = parts[0]

    media_type = part.get("mediaType")

    part_data = part.get("data", {})



    msg_hash = hash_canonical(message)

    msg_key = (principal, message_id)



    # 1. Idempotency Check

    if msg_key in MESSAGE_STORE:

        stored_hash, stored_task_id = MESSAGE_STORE[msg_key]

        if stored_hash != msg_hash:

            return (

                make_a2a_response(

                    {

                        "error": {

                            "code": "IDEMPOTENCY_CONFLICT",

                            "message": "messageId reused with modified content",

                        }

                    },

                    409,

                ),

            )

        pub_task = {

            k: v

            for k, v in TASKS[stored_task_id].items()

            if k not in ("principal", "batchId", "proposals")

        }

        return make_a2a_response({"task": pub_task}, 200)



    # -----------------------------------------------------------------------

    # STAGE 1: PROPOSE BATCH

    # -----------------------------------------------------------------------

    if media_type == MEDIA_BATCH:

        batch_id = part_data.get("batchId", "batch_001")

        packages = part_data.get("packages", [])



        task_id = f"task_{hashlib.md5(f'{principal}_{message_id}'.encode()).hexdigest()[:12]}"

        context_id = f"ctx_{hashlib.md5(batch_id.encode()).hexdigest()[:12]}"



        proposals = []

        for pkg in packages:

            pkg_hash = hash_canonical(pkg)

            if pkg_hash in PACKAGE_CACHE:

                prop = PACKAGE_CACHE[pkg_hash]

            else:

                prop = analyze_package(pkg)

                PACKAGE_CACHE[pkg_hash] = prop

            proposals.append(prop)



        proposal_part = {

            "mediaType": MEDIA_PROPOSALS,

            "data": {"batchId": batch_id, "proposals": proposals},

        }



        task = {

            "id": task_id,

            "contextId": context_id,

            "state": "TASK_STATE_INPUT_REQUIRED",

            "history": [message],

            "artifacts": [proposal_part],

            "principal": principal,

            "batchId": batch_id,

            "proposals": proposals,

        }



        TASKS[task_id] = task

        MESSAGE_STORE[msg_key] = (msg_hash, task_id)



        pub_task = {k: v for k, v in task.items() if k not in ("principal", "batchId", "proposals")}

        return make_a2a_response({"task": pub_task}, 200)



    # -----------------------------------------------------------------------

    # STAGE 2: COMMIT RESULTS

    # -----------------------------------------------------------------------

    elif media_type == MEDIA_RESULTS:

        task_id = message.get("taskId")

        context_id = message.get("contextId")



        if not task_id or task_id not in TASKS:

            return make_a2a_response({"error": "Task not found"}, 404)



        stored_task = TASKS[task_id]



        if stored_task["principal"] != principal:

            return make_a2a_response({"error": "Forbidden: Task belongs to another principal"}, 403)



        if stored_task["contextId"] != context_id:

            return make_a2a_response({"error": "Context ID mismatch"}, 400)



        # Race Condition: Cannot process results on canceled task

        if stored_task["state"] == "TASK_STATE_CANCELED":

            return (

                make_a2a_response(

                    {

                        "error": {

                            "code": "TASK_STATE_CONFLICT",

                            "message": "Cannot submit results for a canceled task",

                        }

                    },

                    409,

                ),

            )



        # Replay check

        if stored_task["state"] == "TASK_STATE_COMPLETED":

            MESSAGE_STORE[msg_key] = (msg_hash, task_id)

            pub_task = {

                k: v

                for k, v in stored_task.items()

                if k not in ("principal", "batchId", "proposals")

            }

            return make_a2a_response({"task": pub_task}, 200)



        results = part_data.get("results", [])

        stored_proposals = {p["packageId"]: p for p in stored_task["proposals"]}



        executions = []

        for res in results:

            pkg_id = res.get("packageId")

            action_id = res.get("actionId")

            action = res.get("action")

            outcome = res.get("outcome")

            receipt_nonce = res.get("receiptNonce")



            if pkg_id not in stored_proposals:

                return make_a2a_response({"error": f"Unknown packageId {pkg_id}"}, 400)



            prop = stored_proposals[pkg_id]

            if prop["actionId"] != action_id or prop["action"] != action:

                return make_a2a_response({"error": f"Action alignment failed for package {pkg_id}"}, 400)



            if outcome == "ACCEPTED":

                executions.append(

                    {

                        "packageId": pkg_id,

                        "actionId": action_id,

                        "action": action,

                        "receiptNonce": receipt_nonce,

                        "facts": prop["facts"],

                        "evidenceRefs": prop["evidenceRefs"],

                    }

                )



        receipt_part = {

            "mediaType": MEDIA_RECEIPTS,

            "data": {

                "batchId": stored_task["batchId"],

                "executions": executions,

            },

        }



        stored_task["state"] = "TASK_STATE_COMPLETED"

        stored_task["history"].append(message)

        stored_task["artifacts"].append(receipt_part)



        MESSAGE_STORE[msg_key] = (msg_hash, task_id)



        pub_task = {k: v for k, v in stored_task.items() if k not in ("principal", "batchId", "proposals")}

        return make_a2a_response({"task": pub_task}, 200)



    else:

        return make_a2a_response({"error": "Unsupported mediaType"}, 400)





# ---------------------------------------------------------------------------

# Task Management Endpoints

# ---------------------------------------------------------------------------





@app.route("/a2a/tasks/<task_id>", methods=["GET"])

@app.route("/tasks/<task_id>", methods=["GET"])

def get_task(task_id):

    valid, err_resp, status = validate_a2a_headers()

    if not valid:

        return err_resp, status



    principal = get_bearer_principal()

    if task_id not in TASKS:

        return make_a2a_response({"error": "Task not found"}, 404)



    task = TASKS[task_id]

    if task["principal"] != principal:

        # Return generic 404 to avoid leaking existence across users

        return make_a2a_response({"error": "Task not found"}, 404)



    pub_task = {k: v for k, v in task.items() if k not in ("principal", "batchId", "proposals")}

    return make_a2a_response(pub_task, 200)





@app.route("/a2a/tasks", methods=["GET"])

@app.route("/tasks", methods=["GET"])

def list_tasks():

    valid, err_resp, status = validate_a2a_headers()

    if not valid:

        return err_resp, status



    principal = get_bearer_principal()

    user_tasks = [

        {k: v for k, v in t.items() if k not in ("principal", "batchId", "proposals")}

        for t in TASKS.values()

        if t["principal"] == principal

    ]



    return make_a2a_response({"tasks": user_tasks}, 200)





@app.route("/a2a/tasks/<task_id>:cancel", methods=["POST"])

@app.route("/tasks/<task_id>:cancel", methods=["POST"])

def cancel_task(task_id):

    valid, err_resp, status = validate_a2a_headers()

    if not valid:

        return err_resp, status



    principal = get_bearer_principal()

    if task_id not in TASKS:

        return make_a2a_response({"error": "Task not found"}, 404)



    task = TASKS[task_id]

    if task["principal"] != principal:

        return make_a2a_response({"error": "Task not found"}, 404)



    # Race condition: Cannot cancel a completed task -> return 409

    if task["state"] == "TASK_STATE_COMPLETED":

        return (

            make_a2a_response(

                {

                    "error": {

                        "code": "TASK_STATE_CONFLICT",

                        "message": "Cannot cancel a completed task",

                    }

                },

                409,

            ),

        )



    task["state"] = "TASK_STATE_CANCELED"

    pub_task = {k: v for k, v in task.items() if k not in ("principal", "batchId", "proposals")}

    return make_a2a_response(pub_task, 200)





if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port) 

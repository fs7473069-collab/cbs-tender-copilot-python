"""Conservative retrieval filtering and topical-relevance assessment."""

import hashlib
import json
import re
import time
from pathlib import Path

import requests
from app.llm.ollama_client import MODEL_NAME
from app.llm.ollama_client import assess_evidence_relevance
from app.llm.ollama_client import assess_evidence_relevance_batch
from app.rag.retriever import KB_DIR, RetrievalError, TenderRetriever


RELEVANCE_CONFIG_VERSION = (
    "lexical-prefilter-v2|relevance-prompt-v1|ws-norm-v1"
)
DEFAULT_CACHE_PATH = KB_DIR / "relevance_cache.json"
_STOP_WORDS = frozenset({
    "about", "against", "also", "and", "are", "been", "being", "by", "can",
    "for", "from", "have", "into", "must", "not", "provide", "shall", "should",
    "that", "the", "their", "this", "with", "will", "system", "supplier",
})
# Conservative synonym expansion reduces false negatives when wording differs
# but the capability is the same. Expansion only adds terms; it never rejects.
_SYNONYM_GROUPS = (
    frozenset({"sso", "signon", "signin"}),
    frozenset({"otp", "onetime", "passcode"}),
    frozenset({"mfa", "2fa", "multifactor", "twofactor"}),
    frozenset({"api", "apis", "endpoint", "endpoints"}),
    frozenset({"gps", "geolocation", "geoposition"}),
    frozenset({"dashboard", "console", "portal"}),
    frozenset({"audit", "logging", "logs"}),
    frozenset({"attendance", "timesheet", "workforce"}),
)
_SHORT_TECH_TOKENS = frozenset({
    "sso", "otp", "mfa", "2fa", "api", "gps", "ui", "ux", "ocr", "ev",
    "ocpp", "kms", "rbac", "vpn", "tls", "ssl", "iam",
})


def _normalize_token(token):
    return token.replace("-", "")


def _meaningful_terms(text):
    raw = [
        _normalize_token(word)
        for word in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)?", text.casefold())
    ]
    terms = set()
    for word in raw:
        if word in _STOP_WORDS:
            continue
        if len(word) >= 4 or word in _SHORT_TECH_TOKENS:
            terms.add(word)
    # Phrase hints for terminology differences that do not share stems.
    if "single" in raw and ("signon" in raw or ("sign" in raw and "on" in raw)):
        terms.update({"sso", "signon"})
    if "one" in raw and "time" in raw and (
        "password" in raw or "passcode" in raw or "otp" in terms
    ):
        terms.update({"otp", "onetime"})
    if "multi" in raw and "factor" in raw:
        terms.update({"mfa", "multifactor"})
    if "two" in raw and "factor" in raw:
        terms.update({"mfa", "twofactor"})
    expanded = set(terms)
    for term in terms:
        for group in _SYNONYM_GROUPS:
            if term in group:
                expanded |= group
    return expanded


def prefilter_candidate(requirement_text, evidence_text):
    """Reject only candidates with no meaningful lexical overlap.

    Passing this gate proves nothing: it merely avoids an LLM call for an
    obviously unrelated passage. A shared word (for example, ``attendance``)
    deliberately passes through to the stricter LLM relevance check.
    Synonym and short-acronym expansion exists only to reduce false negatives.
    """
    requirement_terms = _meaningful_terms(requirement_text)
    evidence_terms = _meaningful_terms(evidence_text)
    overlap = sorted(requirement_terms & evidence_terms)
    if requirement_terms and evidence_terms and not overlap:
        return False, "No meaningful lexical overlap; skipped LLM relevance check."
    return True, "Candidate retained for LLM topical-relevance review."


def _normalize_requirement_key(text):
    """Stable key for in-run reuse of identical requirement wording."""
    return " ".join(text.casefold().split())


RETRIEVAL_OK = {"status": "OK"}
MATCH_NOTE = (
    "Evidence passed similarity, conservative lexical filtering, "
    "and topical relevance checks but still requires human "
    "verification. It does not prove compliance."
)


def _evidence_item(candidate, assessment):
    return {
        "text": candidate["text"],
        "source": candidate["source"],
        "page": candidate["page"],
        "similarity": candidate["score"],
        "kb_chunk_id": candidate.get("kb_chunk_id"),
        "relevance_reason": (assessment or {}).get("reason", ""),
    }


def _match_result(requirement_text, evidence, unassessed, min_similarity, timings):
    """Final status for a search that ran.

    With no accepted evidence, candidates whose relevance check never
    completed (timeout, model error) make the outcome ``NOT_CHECKED``: absence
    of evidence was not established, so it must not read as ``NOT_FOUND``.
    """
    if evidence:
        status, note = "REVIEW_REQUIRED", MATCH_NOTE
    elif unassessed:
        status = "NOT_CHECKED"
        note = (
            f"Relevance assessment did not complete for {unassessed} "
            "retrieved candidate(s); the requirement was not fully checked "
            "against the knowledge base."
        )
    else:
        status, note = "NOT_FOUND", MATCH_NOTE
    return {
        "requirement": requirement_text,
        "status": status,
        "evidence": evidence,
        "note": note,
        "min_similarity": min_similarity,
        "timings": timings,
        "retrieval": dict(RETRIEVAL_OK),
        "relevance": {
            "status": "INCOMPLETE" if status == "NOT_CHECKED" else "COMPLETE",
            "unassessed": unassessed,
        },
    }


def _is_complete(result):
    return (result.get("relevance") or {}).get("status") != "INCOMPLETE"


def retrieval_failure_match(requirement_text, exc, min_similarity=None):
    """Evidence result for a search that could not run.

    ``NOT_CHECKED`` (never ``NOT_FOUND``) so consumers cannot mistake an
    unavailable knowledge base for a genuine absence of evidence.
    """
    code = getattr(exc, "code", None) or "retriever_exception"
    return {
        "requirement": requirement_text,
        "status": "NOT_CHECKED",
        "evidence": [],
        "note": (
            f"Evidence retrieval unavailable ({code}); the requirement was "
            f"not checked against the knowledge base. {exc}"
        ),
        "min_similarity": min_similarity,
        "retrieval": {
            "status": "FAILED",
            "error_code": code,
            "error_type": type(exc).__name__,
        },
    }


def _log_retrieval_failure(exc, requirement_count):
    print(
        "EVIDENCE_RETRIEVAL status=FAILED "
        f"code={getattr(exc, 'code', None) or 'retriever_exception'} "
        f"error_type={type(exc).__name__} requirements={requirement_count}",
        flush=True,
    )


class RequirementEvidenceMatcher:
    """Retrieve evidence, reject obvious mismatches, then check relevance.

    A relevant passage is still only reviewable evidence; it never establishes
    compliance or a company capability by itself.
    """

    def __init__(
        self,
        retriever=None,
        min_similarity=0.55,
        cache_path=None,
        max_accepted_evidence=1,
    ):
        if not 0 <= min_similarity <= 1:
            raise ValueError("min_similarity must be between 0 and 1.")
        if max_accepted_evidence < 1:
            raise ValueError("max_accepted_evidence must be at least 1.")
        self.retriever = retriever or TenderRetriever()
        self.min_similarity = min_similarity
        # One accepted passage is enough for REVIEW_REQUIRED; further sequential
        # relevance LLM calls do not prove compliance and dominate CPU time.
        self.max_accepted_evidence = max_accepted_evidence
        self.cache_path = Path(cache_path) if cache_path else DEFAULT_CACHE_PATH
        self._cache = self._load_cache()
        self._cache_dirty = False
        # Reuse full match results for identical wording within one document run.
        self._match_memo = {}

    def _load_cache(self):
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_cache(self):
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(self.cache_path.suffix + ".tmp")
        temporary.write_text(json.dumps(self._cache, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.cache_path)
        self._cache_dirty = False

    def _flush_cache_if_dirty(self):
        """Persist relevance decisions atomically when the in-memory cache changed."""
        if self._cache_dirty:
            self._save_cache()

    @staticmethod
    def _cache_key(requirement_text, evidence_text):
        stable_input = json.dumps({
            "requirement": " ".join((requirement_text or "").split()),
            "evidence": " ".join((evidence_text or "").split()),
            "model": MODEL_NAME,
            "configuration": RELEVANCE_CONFIG_VERSION,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(stable_input.encode("utf-8")).hexdigest()

    @staticmethod
    def _requirement_text(requirement):
        if isinstance(requirement, str):
            requirement_text = requirement.strip()
        elif isinstance(requirement, dict):
            text = requirement.get("text", "")
            requirement_text = text.strip() if isinstance(text, str) else ""
        else:
            raise ValueError("Requirement must be a string or dictionary.")
        if not requirement_text:
            raise ValueError("Requirement text cannot be empty.")
        return requirement_text

    def _assess(self, requirement_text, evidence_text):
        key = self._cache_key(requirement_text, evidence_text)
        cached = self._cache.get(key)
        if isinstance(cached, dict) and isinstance(cached.get("relevant"), bool):
            return cached, True, 0.0
        started = time.perf_counter()
        assessment = assess_evidence_relevance(requirement_text, evidence_text)
        elapsed = time.perf_counter() - started
        # Never cache invalid, failed, or explicitly unassessed decisions.
        if (
            isinstance(assessment, dict)
            and isinstance(assessment.get("relevant"), bool)
            and not assessment.get("unassessed")
        ):
            self._cache[key] = {
                "relevant": assessment["relevant"],
                "reason": assessment.get("reason", ""),
            }
            self._cache_dirty = True
        return assessment, False, elapsed

    def _retrieve_one(self, requirement_text, top_k):
        try:
            retrieved = self.retriever.retrieve(
                requirement_text, top_k=top_k, include_metrics=True
            )
            if isinstance(retrieved, tuple) and len(retrieved) == 2:
                return retrieved
            return retrieved, {
                "query_embedding_seconds": None,
                "retrieval_seconds": None,
            }
        except TypeError:
            # Supports lightweight legacy/test retrievers without metrics.
            candidates = self.retriever.retrieve(requirement_text, top_k=top_k)
            return candidates, {
                "query_embedding_seconds": None,
                "retrieval_seconds": None,
            }

    def _match_candidates(self, requirement_text, candidates, metrics):
        evidence, llm_seconds = [], 0.0
        filtered, cache_hits, relevance_calls = 0, 0, 0
        skipped_after_accept = 0
        unassessed = 0
        for item in candidates:
            if len(evidence) >= self.max_accepted_evidence:
                # Remaining candidates are not needed for REVIEW_REQUIRED.
                skipped_after_accept += 1
                continue
            if item["score"] < self.min_similarity:
                filtered += 1
                continue
            passes, _filter_reason = prefilter_candidate(requirement_text, item["text"])
            if not passes:
                filtered += 1
                continue
            try:
                assessment, cache_hit, elapsed = self._assess(
                    requirement_text, item["text"]
                )
                cache_hits += int(cache_hit)
                relevance_calls += int(not cache_hit)
                llm_seconds += elapsed
            except (ValueError, OSError, requests.RequestException):
                unassessed += 1
                continue
            if assessment.get("unassessed"):
                unassessed += 1
                continue
            if not assessment["relevant"]:
                continue
            evidence.append(_evidence_item(item, assessment))

        timings = dict(metrics or {})
        timings.update({
            "relevance_llm_seconds": round(llm_seconds, 6),
            "candidates_retrieved": len(candidates),
            "candidates_prefiltered": filtered,
            "relevance_llm_calls": relevance_calls,
            "relevance_cache_hits": cache_hits,
            "candidates_skipped_after_accept": skipped_after_accept,
        })
        return _match_result(
            requirement_text, evidence, unassessed, self.min_similarity, timings
        )

    def match_requirement(self, requirement, top_k=3, *, retrieved=None):
        requirement_text = self._requirement_text(requirement)
        memo_key = _normalize_requirement_key(requirement_text)
        cached_match = self._match_memo.get(memo_key)
        if cached_match is not None and retrieved is None:
            return json.loads(json.dumps(cached_match))

        try:
            if retrieved is None:
                candidates, metrics = self._retrieve_one(requirement_text, top_k)
            elif isinstance(retrieved, tuple) and len(retrieved) == 2:
                candidates, metrics = retrieved
            else:
                candidates = retrieved
                metrics = {
                    "query_embedding_seconds": None,
                    "retrieval_seconds": None,
                }
        except Exception as exc:
            # Not memoized: a later call may succeed once retrieval recovers.
            _log_retrieval_failure(exc, 1)
            return retrieval_failure_match(
                requirement_text, exc, self.min_similarity
            )

        try:
            result = self._match_candidates(requirement_text, candidates, metrics)
            if _is_complete(result):
                self._match_memo[memo_key] = result
            return result
        finally:
            # Durable after each requirement so interrupted chunk rematches
            # still reuse completed relevance decisions.
            self._flush_cache_if_dirty()

    def match_requirements(self, requirements, top_k=3):
        if not isinstance(requirements, list):
            raise ValueError("Requirements must be a list.")

        texts = [self._requirement_text(requirement) for requirement in requirements]
        if not texts:
            return []

        results = [None] * len(texts)
        pending_indices = []
        pending_texts = []
        for index, text in enumerate(texts):
            memo_key = _normalize_requirement_key(text)
            cached_match = self._match_memo.get(memo_key)
            if cached_match is not None:
                results[index] = json.loads(json.dumps(cached_match))
            else:
                pending_indices.append(index)
                pending_texts.append(text)

        memo_hits = len(texts) - len(pending_texts)
        if memo_hits:
            print(
                "Evidence match memo_hits="
                f"{memo_hits} pending={len(pending_texts)}",
                flush=True,
            )

        if not pending_texts:
            return results

        try:
            retrieve_many = getattr(self.retriever, "retrieve_many", None)
            if callable(retrieve_many):
                batch = retrieve_many(
                    pending_texts, top_k=top_k, include_metrics=True
                )
                if isinstance(batch, tuple) and len(batch) == 2:
                    candidates_list, metrics_list = batch
                else:
                    candidates_list = batch
                    metrics_list = [
                        {
                            "query_embedding_seconds": None,
                            "retrieval_seconds": None,
                        }
                        for _ in candidates_list
                    ]
            else:
                candidates_list = []
                metrics_list = []
                for text in pending_texts:
                    candidates, metrics = self._retrieve_one(text, top_k)
                    candidates_list.append(candidates)
                    metrics_list.append(metrics)
            if (
                len(candidates_list) != len(pending_texts)
                or len(metrics_list) != len(pending_texts)
            ):
                raise RetrievalError(
                    "retrieval_count_mismatch",
                    "Retriever returned a result count that does not match "
                    "the number of queries.",
                )
        except Exception as exc:
            _log_retrieval_failure(exc, len(pending_texts))
            for index, text in zip(pending_indices, pending_texts):
                results[index] = retrieval_failure_match(
                    text, exc, self.min_similarity
                )
            return results

        try:
            states = []
            duplicate_indexes = []
            primary_by_key = {}
            for index, text, candidates, metrics in zip(
                pending_indices, pending_texts, candidates_list, metrics_list
            ):
                memo_key = _normalize_requirement_key(text)
                # A duplicate may have been filled earlier in this same batch.
                cached_match = self._match_memo.get(memo_key)
                if cached_match is not None:
                    results[index] = json.loads(json.dumps(cached_match))
                    continue
                if memo_key in primary_by_key:
                    duplicate_indexes.append((index, memo_key))
                    continue
                primary_by_key[memo_key] = index
                states.append({
                    "index": index,
                    "text": text,
                    "candidates": candidates or [],
                    "metrics": dict(metrics or {}),
                    "cursor": 0,
                    "evidence": [],
                    "filtered": 0,
                    "cache_hits": 0,
                    "relevance_calls": 0,
                    "llm_seconds": 0.0,
                    "skipped_after_accept": 0,
                    "unassessed": 0,
                    "done": False,
                })

            while any(not state["done"] for state in states):
                round_jobs = []
                for state in states:
                    if state["done"]:
                        continue
                    job = self._next_relevance_job(state)
                    if job is None:
                        state["done"] = True
                    else:
                        round_jobs.append(job)

                if not round_jobs:
                    break

                uncached = [
                    job for job in round_jobs if job.get("assessment") is None
                ]
                if uncached:
                    # Production path: always batch uncached pairs for this
                    # round (single-pair rounds still go through the batch
                    # helper, which logs and delegates to the single assessor).
                    print(
                        "Evidence relevance matcher: calling "
                        "assess_evidence_relevance_batch "
                        f"uncached_pairs={len(uncached)} "
                        f"round_jobs={len(round_jobs)} "
                        f"active_requirements={sum(1 for s in states if not s['done'])}",
                        flush=True,
                    )
                    started = time.perf_counter()
                    batch_assessments = assess_evidence_relevance_batch([
                        (job["requirement_text"], job["item"]["text"])
                        for job in uncached
                    ])
                    batch_elapsed = time.perf_counter() - started
                    if len(batch_assessments) != len(uncached):
                        raise ValueError(
                            "Batch relevance helper returned a mismatched "
                            f"result count ({len(batch_assessments)} != "
                            f"{len(uncached)})."
                        )
                    share = batch_elapsed / len(uncached)
                    for job, assessment in zip(uncached, batch_assessments):
                        job["assessment"] = assessment
                        job["llm_elapsed"] = share
                        job["cache_hit"] = False
                        if (
                            isinstance(assessment, dict)
                            and isinstance(assessment.get("relevant"), bool)
                            and not assessment.get("unassessed")
                        ):
                            key = self._cache_key(
                                job["requirement_text"], job["item"]["text"]
                            )
                            self._cache[key] = {
                                "relevant": assessment["relevant"],
                                "reason": assessment.get("reason", ""),
                            }
                            self._cache_dirty = True

                for job in round_jobs:
                    state = job["state"]
                    assessment = job["assessment"]
                    if job.get("cache_hit"):
                        state["cache_hits"] += 1
                    else:
                        state["relevance_calls"] += 1
                        state["llm_seconds"] += float(job.get("llm_elapsed") or 0.0)
                    # Unassessed (e.g. batch timeout) is never accepted evidence.
                    if not assessment or assessment.get("unassessed"):
                        state["unassessed"] += 1
                        continue
                    if not assessment.get("relevant"):
                        continue
                    state["evidence"].append(
                        _evidence_item(job["item"], assessment)
                    )

            for state in states:
                if state["cursor"] < len(state["candidates"]) and len(
                    state["evidence"]
                ) >= self.max_accepted_evidence:
                    state["skipped_after_accept"] += (
                        len(state["candidates"]) - state["cursor"]
                    )
                result = self._result_from_state(state)
                memo_key = _normalize_requirement_key(state["text"])
                if _is_complete(result):
                    self._match_memo[memo_key] = result
                results[state["index"]] = result
            primary_results = {
                _normalize_requirement_key(state["text"]): results[state["index"]]
                for state in states
            }
            for index, memo_key in duplicate_indexes:
                # Incomplete results are not memoized, so copy the primary's.
                source = primary_results.get(memo_key) or self._match_memo.get(memo_key)
                if source is not None:
                    results[index] = json.loads(json.dumps(source))
        finally:
            self._flush_cache_if_dirty()
        return results

    def _next_relevance_job(self, state):
        """Advance one state to the next candidate needing a relevance decision.

        Returns None when the requirement is finished (accepted enough evidence
        or no further candidates). Cached decisions are applied inline without
        an LLM call.
        """
        requirement_text = state["text"]
        while state["cursor"] < len(state["candidates"]):
            if len(state["evidence"]) >= self.max_accepted_evidence:
                return None
            item = state["candidates"][state["cursor"]]
            state["cursor"] += 1
            if item["score"] < self.min_similarity:
                state["filtered"] += 1
                continue
            passes, _reason = prefilter_candidate(requirement_text, item["text"])
            if not passes:
                state["filtered"] += 1
                continue
            key = self._cache_key(requirement_text, item["text"])
            cached = self._cache.get(key)
            if isinstance(cached, dict) and isinstance(cached.get("relevant"), bool):
                return {
                    "state": state,
                    "requirement_text": requirement_text,
                    "item": item,
                    "assessment": cached,
                    "cache_hit": True,
                    "llm_elapsed": 0.0,
                }
            return {
                "state": state,
                "requirement_text": requirement_text,
                "item": item,
                "assessment": None,
                "cache_hit": False,
            }
        return None

    def _result_from_state(self, state):
        timings = dict(state.get("metrics") or {})
        timings.update({
            "relevance_llm_seconds": round(state["llm_seconds"], 6),
            "candidates_retrieved": len(state["candidates"]),
            "candidates_prefiltered": state["filtered"],
            "relevance_llm_calls": state["relevance_calls"],
            "relevance_cache_hits": state["cache_hits"],
            "candidates_skipped_after_accept": state["skipped_after_accept"],
        })
        return _match_result(
            state["text"],
            state["evidence"],
            state["unassessed"],
            self.min_similarity,
            timings,
        )

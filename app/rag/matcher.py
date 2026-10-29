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
from app.rag.retriever import KB_DIR, TenderRetriever


RELEVANCE_CONFIG_VERSION = "lexical-prefilter-v2|relevance-prompt-v1"
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
            "requirement": requirement_text,
            "evidence": evidence_text,
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
        # Never cache an invalid or failed decision.
        if isinstance(assessment, dict) and isinstance(assessment.get("relevant"), bool):
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
                filtered += 1
                continue
            if not assessment["relevant"]:
                continue
            evidence.append({
                "text": item["text"],
                "source": item["source"],
                "page": item["page"],
                "similarity": item["score"],
                "relevance_reason": assessment.get("reason", ""),
            })

        timings = dict(metrics or {})
        timings.update({
            "relevance_llm_seconds": round(llm_seconds, 6),
            "candidates_retrieved": len(candidates),
            "candidates_prefiltered": filtered,
            "relevance_llm_calls": relevance_calls,
            "relevance_cache_hits": cache_hits,
            "candidates_skipped_after_accept": skipped_after_accept,
        })
        return {
            "requirement": requirement_text,
            "status": "REVIEW_REQUIRED" if evidence else "NOT_FOUND",
            "evidence": evidence,
            "note": (
                "Evidence passed similarity, conservative lexical filtering, "
                "and topical relevance checks but still requires human "
                "verification. It does not prove compliance."
            ),
            "min_similarity": self.min_similarity,
            "timings": timings,
        }

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
        except RuntimeError as exc:
            return {
                "requirement": requirement_text,
                "status": "NOT_FOUND",
                "evidence": [],
                "note": f"Evidence matching was not run: {exc}",
                "min_similarity": self.min_similarity,
            }

        try:
            result = self._match_candidates(requirement_text, candidates, metrics)
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
        except RuntimeError as exc:
            note = f"Evidence matching was not run: {exc}"
            for index, text in zip(pending_indices, pending_texts):
                results[index] = {
                    "requirement": text,
                    "status": "NOT_FOUND",
                    "evidence": [],
                    "note": note,
                    "min_similarity": self.min_similarity,
                }
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
                    started = time.perf_counter()
                    batch_assessments = assess_evidence_relevance_batch([
                        (job["requirement_text"], job["item"]["text"])
                        for job in uncached
                    ])
                    batch_elapsed = time.perf_counter() - started
                    share = batch_elapsed / len(uncached)
                    for job, assessment in zip(uncached, batch_assessments):
                        job["assessment"] = assessment
                        job["llm_elapsed"] = share
                        job["cache_hit"] = False
                        if isinstance(assessment, dict) and isinstance(
                            assessment.get("relevant"), bool
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
                    if not assessment or not assessment.get("relevant"):
                        continue
                    state["evidence"].append({
                        "text": job["item"]["text"],
                        "source": job["item"]["source"],
                        "page": job["item"]["page"],
                        "similarity": job["item"]["score"],
                        "relevance_reason": (assessment or {}).get("reason", ""),
                    })

            for state in states:
                if state["cursor"] < len(state["candidates"]) and len(
                    state["evidence"]
                ) >= self.max_accepted_evidence:
                    state["skipped_after_accept"] += (
                        len(state["candidates"]) - state["cursor"]
                    )
                result = self._result_from_state(state)
                self._match_memo[_normalize_requirement_key(state["text"])] = result
                results[state["index"]] = result
            for index, memo_key in duplicate_indexes:
                cached_match = self._match_memo.get(memo_key)
                if cached_match is not None:
                    results[index] = json.loads(json.dumps(cached_match))
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
        return {
            "requirement": state["text"],
            "status": "REVIEW_REQUIRED" if state["evidence"] else "NOT_FOUND",
            "evidence": state["evidence"],
            "note": (
                "Evidence passed similarity, conservative lexical filtering, "
                "and topical relevance checks but still requires human "
                "verification. It does not prove compliance."
            ),
            "min_similarity": self.min_similarity,
            "timings": timings,
        }

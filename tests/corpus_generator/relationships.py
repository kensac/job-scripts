from __future__ import annotations

from typing import Any

from api import db
from tests.corpus_generator.rows import _Generator

# --- the handful of columns whose value is a function of the corpus ---------
#
# Not shapes and not schema: values that must agree with ANOTHER value in the
# same corpus, or the row is incoherent whatever its distribution says. Each
# one is here because something reads the relationship, and the list is short
# on purpose - a corpus that reproduced every invariant would make the tests
# over it tautologies.


def _specialise(table: str, rows: list[dict[str, Any]], gen: _Generator) -> None:
    if table == "users":
        for i, row in enumerate(rows):
            row["sub"] = f"corpus-user-{i}"
            row["email"] = f"user{i}@corpus.invalid"
            row["name"] = f"Corpus User {i}"
            # Every group the app itself knows about, spread across the users,
            # plus the measured ones. /job-scripts aggregates BY group, and
            # over a population of one every such number is the same whether
            # the query is right or wrong.
            row["groups"] = sorted(
                {
                    ["infra-admins", "jobtracker-users-internal"][i % 2],
                    *row.get("groups", []),
                }
            )
    elif table == "jobs":
        for i, row in enumerate(rows):
            row["url"] = f"https://corpus.invalid/{row['source']}/{i}"
            row["raw_url"] = row["url"] + "?utm_source=corpus"
    elif table == "user_filters":
        from core.filters import build_custom_instructions, compute_prompt_hash

        for i, row in enumerate(rows):
            row["name"] = f"corpus filter {i}"
            # Distinct per filter, so distinct prompt_hashes: two enabled
            # filters sharing a hash makes the board's filter gate
            # unsatisfiable, and a corpus that shipped that would leave every
            # board empty for a reason nothing points at.
            kind = ["backend", "frontend", "data", "ml"][i % 4]
            row["prompt"] = f"must be a {kind} role, requirement {i}"
            # Computed with the shipping template, because a stored hash that
            # does not match one is a filter whose board silently empties.
            row["prompt_hash"] = compute_prompt_hash(
                build_custom_instructions(row["prompt"], row["on_ambiguous"])
            )
        # The measured rate is 10% enabled - production holds ten filters and
        # runs one - and drawn per row that leaves most users with none at all.
        # A user with no enabled filter gets no board, because materialisation
        # requires at least one, so drawing it independently would leave the
        # whole board path unreached in most runs and reached in some: a
        # corpus that is sometimes vacuous is worse than one that always is.
        # The rate is kept, the floor of one per user is added.
        by_user: dict[Any, list[dict[str, Any]]] = {}
        for row in rows:
            by_user.setdefault(row["user_id"], []).append(row)
        rate = gen.profile["tables"]["user_filters"]["columns"]["enabled"]["true_rate"]
        for owned in by_user.values():
            for i, row in enumerate(owned):
                row["enabled"] = i < max(1, round(rate * len(owned)))
    elif table == "ai_batches":
        for i, row in enumerate(rows):
            row["provider_batch_id"] = f"batch_corpus_{i:08d}"
    elif table == "email_messages":
        for i, row in enumerate(rows):
            row["provider_message_id"] = f"corpus-msg-{i}"
            row["from_email"] = f"careers@company{i % 97}.invalid"
            row["from_name"] = f"Company {i % 97} Recruiting"
            row["subject"] = f"Your application to Company {i % 97}"
            row["to_emails"] = [f"user{row['user_id']}@corpus.invalid"]
    elif table == "user_oauth_tokens":
        from api import crypto

        for row in rows:
            # Real ciphertext under the suite's own key, so the paths that
            # decrypt a token are exercised rather than mocked. This answers
            # the ticket's open question: nothing about the encrypted material
            # forces those paths to stay mocked.
            row["refresh_token_enc"] = crypto.encrypt(f"corpus-refresh-{row['user_id']}")
            row["access_token_enc"] = crypto.encrypt(f"corpus-access-{row['user_id']}")
            row["provider"] = "google"
            row["account_email"] = f"user{row['user_id']}@corpus.invalid"
    elif table == "user_settings":
        from api import crypto

        for row in rows:
            row["api_key_enc"] = crypto.encrypt("corpus-api-key")
            row["digest_token"] = f"corpus-digest-{row['user_id']}"
    elif table == "ai_queries":
        _pair_verdicts_into_sweeps(rows, gen)
    elif table == "user_jobs":
        # A board row exists for one of two reasons, and they are not
        # interchangeable: the user TOUCHED the job, or materialisation added
        # it because the job passes every filter. Generating both kinds by
        # distribution would make "the board is not empty" true whether or not
        # materialisation ran at all, and the tests over the write-time
        # predicate would stop being able to fail.
        #
        # So the generator writes only touched rows, and every untouched row in
        # the corpus is one the application's own predicate put there.
        statuses = list(
            gen.profile["tables"]["user_jobs"]["columns"]["status"].get("values") or {"Applied": 1}
        )
        for i, row in enumerate(rows):
            row["status"] = row.get("status") or statuses[i % len(statuses)]
    elif table == "job_embeddings":
        from core.derivations.embeddings import EMBEDDING_DIMENSIONS, EMBEDDING_MODEL

        for row in rows:
            # pgvector will not take anything else, and the profile cannot
            # describe a vector without carrying one. The axis is jittered so
            # nearest-neighbour ordering is deterministic but not degenerate.
            vector = [1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1)
            vector[1] = gen.rng.random()
            row["embedding"] = str(vector)
            row["model"] = EMBEDDING_MODEL
            row["content_hash"] = f"corpus-{gen._next():08x}"
    elif table == "job_comp":
        for row in rows:
            lo, hi = row.get("comp_min"), row.get("comp_max")
            if lo is not None and hi is not None and lo > hi:
                row["comp_min"], row["comp_max"] = hi, lo


def _pair_verdicts_into_sweeps(rows: list[dict[str, Any]], gen: _Generator) -> None:
    """Pair verdicts into complete posting sweeps.

    Board membership needs closed, clearance and enabled custom verdicts
    together. Independent draws once produced zero materialized board rows,
    although board tests still passed. Pairing restores the pipeline's
    relationship while retaining measured row counts, check types and status
    distributions.
    """
    urls = gen.pools.get(("jobs", "url")) or []
    hashes = [
        r["prompt_hash"]
        for r in db.query("SELECT prompt_hash FROM user_filters WHERE enabled ORDER BY id")
    ]
    if not urls or not hashes:
        return

    custom = [r for r in rows if r.get("check_type") == "custom"]
    # Self-sizing: as many swept jobs as there are custom verdicts to go round,
    # so every swept job gets a complete set and none gets a partial one.
    swept = urls[: max(1, len(custom) // len(hashes))]
    for i, row in enumerate(custom):
        row["url"] = swept[i // len(hashes)] if i // len(hashes) < len(swept) else row["url"]
        row["prompt_hash"] = hashes[i % len(hashes)]

    # The structural gates the same jobs have to clear. Reassigning existing
    # rows rather than adding any, so the check_type mix is untouched.
    for check in ("closed", "clearance"):
        gates = [r for r in rows if r.get("check_type") == check]
        for url, row in zip(swept, gates, strict=False):
            row["url"] = url

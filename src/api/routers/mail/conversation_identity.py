"""Provider message identity shared by thread reads and assignments."""

# How many messages one assignment may carry. A conversation is a handful of
# messages; a number far above that means the grouping is wrong rather than the
# thread being long, so a larger group is assigned one message at a time. On
# 2026-10-31, 13 of 1,494 multi-message conversation keys in production held
# more than this (878 messages, the largest 165).
MAX_THREAD_FANOUT = 40

# A conversation's key, derived rather than stored.
#
# `provider_thread_id` is the FIRST References entry, which is the Message-ID
# of the message that started the thread. So replies carry it and the ROOT does
# not - a first message has no References, by definition. That left 1,113
# messages as the origin of a thread that exists in the database without being
# part of it: assigning a reply moved its siblings and silently left the
# original behind.
#
# Coalescing to the message's own id closes it, because a root's Message-ID IS
# the key its replies carry. A message with no thread and no replies coalesces
# to its own id and matches nothing else, so this cannot group unrelated mail -
# which is the failure that made subject-based grouping unsafe.
CONVERSATION_KEY_SQL = "COALESCE(m.provider_thread_id, m.provider_message_id)"

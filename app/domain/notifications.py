"""What a school's messages say.

Pure: facts in, a body out. No gateway, no session, no settings lookup — which
is what makes the wording testable, and the wording is the part that matters.
A parent who gets one of these messages may never open the portal at all, so
for many families it is the product.

Two constraints shape everything here.

**A message is paid for by the segment.** 160 characters of GSM-7 is one
segment; the 161st doubles the cost of every message in the run, and a school
sending 400 fee reminders a term is counting them. So each message has a
budget, and anything that will not fit sheds its least important clause rather
than running over or being cut mid-word. One non-GSM-7 character — a curly
apostrophe pasted from a document, say — drops the whole segment to 70
characters, so the text here stays plain ASCII and `fits_one_segment` is what
the tests assert.

**A parent reads their school's language.** `school.locale` already exists and
already defaults to `en`, so this is a choice a school makes, not a guess made
here. Kiswahili is the one that matters in practice and it is the reason the
copy below is short: a translation is usually longer than its English, and a
message that fits in English and not in Kiswahili is a bug a Tanzanian parent
pays for.
"""

from __future__ import annotations

from dataclasses import dataclass

#: One GSM-7 segment. Past this, the gateway bills two.
SEGMENT = 160

#: The characters a GSM-7 segment can hold. Anything outside it forces UCS-2
#: and a 70-character segment, so composed bodies are checked against it.
_GSM7 = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
    "^{}\\[~]|€"
)


def fits_one_segment(body: str) -> bool:
    """Whether this costs one message or two.

    The escaped GSM-7 characters (`^{}\\[~]|€`) take two septets each, which is
    why this counts rather than calling `len`.
    """
    if not set(body) <= _GSM7:
        # UCS-2: every character costs the same and there are fewer of them.
        return len(body) <= 70
    return sum(2 if c in "^{}\\[~]|€" else 1 for c in body) <= SEGMENT


def _assemble(essential: str, *optional: str) -> str:
    """The essential clause, plus as many of the optional ones as fit.

    Dropping a sentence is better than two things: running into a second
    segment, which costs the school money it did not agree to, and truncating,
    which is how a parent receives half an admission number.

    The optional clauses are offered in the order they matter, and they are
    dropped from the end, so the most useful survivor is kept.
    """
    body = essential
    for clause in optional:
        candidate = f"{body} {clause}"
        if fits_one_segment(candidate):
            body = candidate
    return body


def _is_swahili(locale: str | None) -> bool:
    return (locale or "en").strip().lower().startswith("sw")


@dataclass(frozen=True, slots=True)
class EnrolmentFacts:
    """What the message about a newly enrolled child needs to carry."""

    child_first_name: str
    admission_no: str
    school_short_name: str
    #: Where the guardian signs in. Already a bare host in practice, because
    #: "https://" is eleven characters a parent does not need to be sent.
    portal: str


def enrolment_confirmed(facts: EnrolmentFacts, *, locale: str | None = "en") -> str:
    """"Your child is in, here is the number, here is how to get in."

    The admission number is the essential part and never dropped: it is what a
    parent is asked for at the bursar's desk, at the uniform shop and on every
    form for the next seven years. The invitation to sign in goes first if
    something has to go, because a parent who cannot find the portal can still
    walk into the school with the number.
    """
    child = facts.child_first_name.strip()
    school = facts.school_short_name.strip()

    if _is_swahili(locale):
        return _assemble(
            f"{child} amesajiliwa {school}. Namba ya usajili: {facts.admission_no}.",
            f"Ingia {facts.portal} kwa namba hii ya simu kuona ada na matokeo.",
        )

    return _assemble(
        f"{child} is enrolled at {school}. Admission number: {facts.admission_no}.",
        f"Sign in at {facts.portal} with this phone number to see fees and results.",
    )

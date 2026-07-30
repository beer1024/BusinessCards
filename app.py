#!/usr/bin/env python3
import http.server
import socketserver
import json
import os
import re
import string
import traceback
import urllib.parse

API_KEY = os.environ.get("GROQ_API_KEY", "")
if not API_KEY:
    print("WARNING: GROQ_API_KEY is not set!")

from groq import Groq
client = Groq(api_key=API_KEY) if API_KEY else None

PORT = 8000

# ---------------------------------------------------------------------------
# FRONT SIDE — the LLM stays in charge here. This is the part you liked, so
# its behavior is unchanged aside from trimming the prompt to front-only
# concerns (it no longer needs to know anything about the back checklist).
# ---------------------------------------------------------------------------

FRONT_SYSTEM_PROMPT = """
You are a decisive Creative Director helping a customer design the FRONT of a
business card. Usually this centers on their uploaded image, but sometimes the
customer has no image and is doing a text-only front instead.

You control ONLY these fields:
- front_layout (a short description of the front layout/composition)
- style_vibe (a short description of the overall visual style)
- image_fit ("contain" or "cover")
- image_scale (a number between 0.3 and 1.0, how large the image appears)
- image_position_x ("left", "center", or "right")
- image_position_y ("top", "middle", or "bottom")
- background_mode ("extend", "white", or "solid")
- background_color (a hex color, only used when background_mode is "solid")
- has_lace_accents (true/false, decorative accents)
- image_declined (true/false — whether the customer is doing a text-only
  front with no image)

You OWN the image_declined flag — decide it yourself from the conversation:
- Set it to true the moment the customer indicates, in any wording, that
  they don't have an image or want to skip it (e.g. "I don't have one", "skip
  the image", "we're text-only", "no image for now"). When you set it true,
  don't ask for an image again — steer the conversation toward typography/text
  layout ideas instead, and let reply_to_user acknowledge the switch (e.g.
  offer bold lettering of the business name, or ask about a style they have
  in mind).
- Set it back to false if the customer later says, in any wording, that they
  do have an image after all or want to add one (e.g. "actually I do have a
  logo", "found one, hold on") — acknowledge it and invite them to upload it.
- Otherwise, keep it unchanged from the current design_spec you were given.
- The "Image uploaded" flag in the context reflects whether a file was
  actually uploaded; use it together with the customer's words, not instead
  of them.
- IMPORTANT — don't confuse "no changes needed" with "no image at all": if
  "Image uploaded" is true (an image is already attached) and you just
  asked whether they'd like to adjust the size, reposition it, or make it
  full-bleed, a bare/short negative reply ("no", "nope", "nah", "no
  thanks", "not really") means "no adjustments needed, leave the image as
  it is" — it does NOT mean they want to remove the image or go
  text-only. Once an image is already attached, only set image_declined
  to true if the customer *explicitly* asks to remove or replace the
  image (e.g. "actually, let's skip the image", "remove it, text-only
  please", "I don't want the image anymore") — a bare "no" alone is never
  enough to flip it once an image exists.
- Symmetrically, if you just asked whether they'd like to adjust the size,
  reposition it, or make it full-bleed, and the customer replies with a
  bare/unspecific affirmative ("yes", "yeah", "sure", "ok") without saying
  *which* of the three they mean, do NOT guess or ask a vague open-ended
  question — reply_to_user must ask a closed follow-up that re-names the
  same three options (e.g. "Great — which would you like: resize it,
  reposition it, or make it full-bleed?"). If their reply already names
  which one they want (e.g. "yes, make it bigger", "full bleed please"),
  just make that change directly as usual — this follow-up only applies
  when the affirmative is genuinely unspecific.

When the customer gives you a sizing instruction, always turn it into a
concrete image_scale number (clamped to 0.3-1.0) — never reply with vague
instructions back to them. Worked examples, using the CURRENT image_scale
from the design_spec you were given as the starting point:
- "reduce size by 50%" / "make it 50% smaller" -> new_scale = current_scale
  x 0.5
- "increase size by 20%" / "make it 20% bigger" -> new_scale = current_scale
  x 1.2
- A bare number on its own, like ".5", "0.5", or "50%" (especially right
  after you asked about size, or right after the customer said "smaller"/
  "bigger") -> treat it as the new image_scale directly (0.5 in this
  example), not as a request for more instructions.
- Vaguer phrases with no number ("make it bigger", "make it smaller") ->
  keep using your own judgment for a reasonable step (e.g. +/-0.2 to 0.3),
  same as before.
Always confirm the concrete result in reply_to_user (e.g. "Done - I've
resized it to half its previous size.") rather than describing the
adjustment as something the customer still needs to do.

Never mention or modify contact information or anything about the back of
the card. That is handled separately by a different system.

If the customer already went through the text-only content checklist
(business name, tagline, social/QR — handled by a separate Python-owned
step before you're ever invoked), their content is already decided and
locked in. Do not ask about business name, tagline, or social/QR again —
your job at that point is strictly the visual side: layout, style, color,
spacing, size, and any lace/decorative accents. If the customer wants to
change the actual content (e.g. "change my tagline to..."), that's out of
your scope — acknowledge it but note the content questions were already
answered separately; don't silently overwrite it yourself.

You also decide "front_confirmed" (true/false) — whether the customer is
done with the front and ready to move on to the back of the card:
- Set it to true if the customer expresses, in ANY wording, that they're
  happy with the front and ready to proceed — not just an exact phrase.
  Examples that all count: "i like it", "you got it", "yeppers", "this
  works", "ship it", "let's move on", "good enough", "perfect, next",
  "that'll do". Use your judgment for tone/intent, not a fixed word list.
- Keep it false if the message ALSO contains a change/adjustment request
  (e.g. "looks good, just move it left a bit", "i like it but make it
  smaller") — make the requested change as usual in that case, don't treat
  it as final yet.
- Keep it false for anything that isn't clearly an
  approval-and-ready-to-proceed signal — small talk, questions, or a
  vague/ambiguous reply should leave this false and just get a normal
  reply_to_user.
- Default to false when in doubt.

Always return ONLY this exact JSON shape, nothing before or after it:
{
  "reply_to_user": "a short, confident reply to the customer",
  "design_spec": {
    "front_layout": "",
    "style_vibe": "",
    "image_fit": "contain",
    "image_scale": 1.0,
    "image_position_x": "center",
    "image_position_y": "middle",
    "background_mode": "extend",
    "background_color": "#ffffff",
    "has_lace_accents": false,
    "image_declined": false,
    "front_confirmed": false
  }
}
"""

# Deterministic safety net for handle_front_side (see comment there) — a
# bare, standalone negative reply, matched on the WHOLE message (not just
# a substring) so a longer message like "no, actually let's skip the image
# entirely" is deliberately left to the LLM to interpret instead.
BARE_NEGATIVE_REPLIES = {
    "no", "nope", "nah", "nay", "no thanks", "no thank you", "not really",
    "not right now", "no i'm good", "no im good", "nah it's fine",
    "nah its fine", "no it's fine", "no its fine",
}


def _is_bare_negative(user_message):
    lower = user_message.strip().lower().strip(" .!")
    return lower in BARE_NEGATIVE_REPLIES

# ---------------------------------------------------------------------------
# BACK SIDE — Python owns the order and the questions. The LLM is only asked
# to interpret a single answer for a single field. This is what fixes the
# "agent forgets to ask the next question" problem: the question that gets
# asked is computed here, not generated by the model.
# ---------------------------------------------------------------------------

BACK_FIELDS = [
    {"key": "name",     "label": "your full name",        "type": "text",
     "question": "Do you want your full name on the back?"},
    {"key": "title",    "label": "a job title",           "type": "text",
     "question": "Do you want a job title on the back?"},
    {"key": "company",  "label": "a company name",        "type": "text",
     "question": "Do you want a company name on the back?"},
    {"key": "phone",    "label": "a phone number",        "type": "text",
     "question": "Do you want a phone number on the back?"},
    {"key": "email",    "label": "an email address",      "type": "text",
     "question": "Do you want an email address on the back?"},
    {"key": "website",  "label": "a website",             "type": "text",
     "question": "Do you want a website URL on the back?"},
    {"key": "address",  "label": "a physical address",    "type": "text",
     "question": "Do you want a physical address on the back?"},
    {"key": "social",   "label": "social media handles",  "type": "text",
     "question": "Do you want social media handles on the back?"},
    {"key": "qr_code",  "label": "a QR code",             "type": "bool",
     "question": "Do you want a QR code on the back?"},
    {"key": "tagline",  "label": "a tagline",             "type": "text",
     "question": "Do you want a tagline or slogan on the back?"},
    {"key": "services", "label": "a short list of services", "type": "text",
     "question": "Do you want a short list of services on the back?"},
    {"key": "cta",      "label": "a call to action",      "type": "text",
     "question": "Do you want a call-to-action, like 'Call now' or 'Visit us online'?"},
]

# ---------------------------------------------------------------------------
# BACK TEMPLATE PICKER — a closed-ended gate asked once, right before the
# 12 field questions start, so the customer picks how their answered fields
# will be visually arranged. Every template can still render every field the
# customer answers "yes" to (nothing is ever hidden) — comfortable_field_count
# is only used later to offer switching to a roomier layout if they end up
# filling in more than a template comfortably shows.
# ---------------------------------------------------------------------------

BACK_TEMPLATES = [
    {"id": "classic_centered", "label": "Classic Centered",
     "description": "Everything centered with generous spacing — timeless and formal",
     "keywords": ["classic", "centered", "center", "formal", "timeless"],
     "comfortable_field_count": 6},
    {"id": "left_block", "label": "Left-Aligned Block",
     "description": "Bold name/title/company header with a left-aligned contact list below",
     "keywords": ["left", "block", "left-aligned", "left aligned"],
     "comfortable_field_count": 8},
    {"id": "icon_list", "label": "Icon List",
     "description": "A vertical list with a small icon next to each contact detail",
     "keywords": ["icon", "icons", "list"],
     "comfortable_field_count": 9},
    {"id": "split_two_column", "label": "Split Two-Column",
     "description": "Contact info on one side, QR code or image on the other, divided by a line",
     "keywords": ["split", "two column", "two-column", "column"],
     "comfortable_field_count": 7},
    {"id": "top_banner", "label": "Top Banner",
     "description": "A solid color band across the top with name and title, remaining details below",
     "keywords": ["banner", "band", "top banner"],
     "comfortable_field_count": 8},
    {"id": "bordered_frame", "label": "Bordered Frame",
     "description": "A decorative border framing the centered content",
     "keywords": ["border", "frame", "bordered", "framed"],
     "comfortable_field_count": 6},
    {"id": "bottom_corner", "label": "Bottom-Corner Minimal",
     "description": "Just the essentials tucked in a corner, with lots of white space",
     "keywords": ["corner", "minimal", "minimalist", "simple"],
     "comfortable_field_count": 3},
    {"id": "qr_forward", "label": "QR-Forward",
     "description": "A large QR code (or image) as the centerpiece with brief text below",
     "keywords": ["qr", "qr code", "qr-forward", "scan"],
     "comfortable_field_count": 3},
]
BACK_TEMPLATE_BY_ID = {t["id"]: t for t in BACK_TEMPLATES}

BACK_TEMPLATE_QUESTION = (
    "How would you like the back laid out? Pick a style: "
    + "; ".join(f"{i + 1}) {t['label']}" for i, t in enumerate(BACK_TEMPLATES))
    + ". (You can click one of the previews, or just tell me the name or number.)"
)

SKIP_WORDS = {
    "no", "nope", "skip", "none", "n/a", "na", "nah", "not needed",
    "no thanks", "no thank you", "not really", "never mind", "pass",
}
YES_WORDS = {
    "yes", "yeah", "yep", "yup", "sure", "please", "yes please",
    "of course", "definitely", "add it", "add one",
}
CONFIRM_YES_WORDS = {
    "yes", "yeah", "yep", "yup", "correct", "right", "that's right",
    "thats right", "sounds good", "perfect", "good", "confirm", "confirmed",
    "correct that's it", "yes that's right",
}
CONFIRM_NO_WORDS = {
    "no", "nope", "wrong", "incorrect", "that's wrong", "thats wrong",
    "not right", "not correct", "that's not right",
}
EDIT_TRIGGERS = [
    "change", "actually", "correct", "wrong", "fix", "edit", "instead",
    "meant", "update", "redo", "mistake", "not right",
]

FIELD_BY_KEY = {f["key"]: f for f in BACK_FIELDS}


# Common lead-in phrases people use when answering — stripped so the stored
# value is just the actual content, e.g. "yeah it's ed brown" -> "ed brown".
# Deliberately conservative: if nothing matches, the raw text is kept as-is
# rather than guessed at, so we never invent something that wasn't said.
LEAD_IN_PATTERNS = [
    r"^(yeah|yep|yup|sure|ok|okay)[,]?\s+",
    r"^(it'?s|it is|that'?s|that is|thats)\s+",
    r"^(no[,]?\s+)?(the\s+)?(name|title|company( name)?|phone( number)?|"
    r"email( address)?|website|address|social( media)?|tagline|"
    r"services?|cta|call to action)\s+is\s+",
    r"^my\s+(name|title|company( name)?)\s+is\s+",
]


def _starts_with_phrase(lower, phrase):
    """True if `lower` begins with `phrase` as whole words — not just a
    matching prefix of characters. Plain str.startswith("no") would treat
    "nonya@bidness.com" as starting with "no", which is wrong; this checks
    word-by-word instead. Each word has surrounding punctuation stripped
    first, so "no," / "no." / "no!" etc. still count as the word "no"."""
    phrase_words = phrase.split()
    words = [w.strip(string.punctuation) for w in lower.split()]
    return words[:len(phrase_words)] == phrase_words


def clean_value(raw):
    text = raw.strip()
    # Peel off a leading correction prefix like "no, ..." first (the \b
    # word boundary matters — without it this would also strip "no" off
    # the front of "nonya@bidness.com", turning it into "nya@bidness.com").
    text = re.sub(r"^(no|nope)\b[,]?\s*", "", text, flags=re.IGNORECASE).strip()
    for pattern in LEAD_IN_PATTERNS:
        match = re.match(pattern, text, flags=re.IGNORECASE)
        if match:
            text = text[match.end():].strip()
    # Strip wrapping quote marks (straight or curly) if the whole value is
    # quoted.
    text = text.strip("\"'\u201c\u201d\u2018\u2019 ")
    return text


def fallback_extract(user_message, field_type):
    """Deterministic extraction — no LLM involved. This is intentional:
    an earlier version asked an LLM to "clean up" the value, and it would
    occasionally truncate real answers (dropping words it mistook for
    filler) or, worse, invent a plausible-sounding value out of nothing
    when the message didn't actually contain one. Simple string cleanup
    can't do that — worst case it's too literal, never fabricated."""
    msg = user_message.strip()
    lower = msg.lower().strip(" .!")

    if lower in SKIP_WORDS or any(_starts_with_phrase(lower, w) for w in SKIP_WORDS):
        return {"skip": True, "value": False if field_type == "bool" else ""}

    if field_type == "bool":
        value = lower in YES_WORDS or _starts_with_phrase(lower, "yes")
        return {"skip": False, "value": value}

    return {"skip": False, "value": clean_value(msg)}


def extract_back_answer(user_message, field):
    return fallback_extract(user_message, field["type"])


def classify_confirmation(message):
    lower = message.lower().strip(" .!")
    if lower in CONFIRM_YES_WORDS or any(_starts_with_phrase(lower, w) for w in CONFIRM_YES_WORDS):
        return "yes"
    if lower in CONFIRM_NO_WORDS or any(_starts_with_phrase(lower, w) for w in CONFIRM_NO_WORDS):
        return "no"
    return "unclear"


def next_unanswered_field(contact):
    for field in BACK_FIELDS:
        if contact.get(field["key"], None) is None:
            return field
    return None


def detect_edit_request(message, contact, context_key):
    """Looks for something like 'actually, change my company name' —
    a request to correct a field that's already been answered, even if
    the conversation has since moved on to a different field."""
    lower = message.lower()
    if not any(trigger in lower for trigger in EDIT_TRIGGERS):
        return None

    for field in BACK_FIELDS:
        if field["key"] == context_key:
            continue
        if contact.get(field["key"]) is None:
            continue  # only already-answered fields can be corrected this way
        label_words = [w for w in field["label"].replace("a ", "").replace("your ", "").split() if len(w) > 3]
        if field["key"] in lower or any(w in lower for w in label_words):
            return field
    return None


def ask_for_value(field, contact):
    reply = f"Sure — what should {field['label']} be?"
    update = {
        "contact": contact,
        "pending_field": field["key"],
        "pending_stage": "await_value",
        "pending_value": None,
        "back_complete": next_unanswered_field(contact) is None,
    }
    return reply, update


def propose_confirmation(field, extraction, contact):
    if extraction.get("skip"):
        pending_value = False if field["type"] == "bool" else ""
        if field["type"] == "bool":
            reply = "Just to confirm — no QR code, correct?"
        else:
            reply = f"Just to confirm — skip {field['label']} entirely, correct?"
    else:
        value = extraction.get("value")
        pending_value = value
        if field["type"] == "bool":
            reply = f"Just to confirm — {'add' if value else 'skip'} a QR code, correct?"
        else:
            reply = f"So {field['label']} will be: {value}\n\nIs that right?"

    update = {
        "contact": contact,
        "pending_field": field["key"],
        "pending_stage": "await_confirm",
        "pending_value": pending_value,
        "back_complete": next_unanswered_field(contact) is None,
    }
    return reply, update


def commit_value(field, value, contact, back_template=None):
    contact[field["key"]] = value
    if field["key"] == "qr_code" and value:
        # Saying "yes" to a QR code no longer just locks in a bool — it opens
        # a small upload-vs-generate sub-flow (see handle_qr_subflow) before
        # moving on to the next field.
        return start_qr_subflow(contact)
    return finish_or_advance(contact, back_template)


def finish_or_advance(contact, back_template=None):
    """Shared "what's next" step used after any field (or the QR sub-flow)
    is committed: either ask the next unanswered field, or — once every
    field has been resolved — hand off to finish_back_fields, which checks
    whether the chosen template comfortably fits everything collected."""
    upcoming = next_unanswered_field(contact)
    if upcoming:
        update = {
            "contact": contact,
            "pending_field": None,
            "pending_stage": None,
            "pending_value": None,
            "back_complete": False,
            "capacity_check_pending": False,
        }
        return f"Locked in.\n\n{upcoming['question']}", update
    return finish_back_fields(contact, back_template)


def count_filled_back_fields(contact):
    """How many of the 12 fields the customer actually kept (answered
    "yes"/provided a value), as opposed to skipping."""
    count = 0
    for field in BACK_FIELDS:
        value = contact.get(field["key"])
        if field["type"] == "bool":
            if value:
                count += 1
        else:
            if value:
                count += 1
    return count


def suggest_roomier_templates(current_id, needed_count):
    """The 1-2 templates (other than the current one) best suited to fit
    `needed_count` fields, preferring the smallest capacity that still fits."""
    others = [t for t in BACK_TEMPLATES if t["id"] != current_id]
    fits = [t for t in others if t["comfortable_field_count"] >= needed_count]
    fits.sort(key=lambda t: t["comfortable_field_count"])
    if fits:
        return fits[:2]
    # Nothing comfortably fits everything — offer the two roomiest options.
    return sorted(others, key=lambda t: -t["comfortable_field_count"])[:2]


def finish_back_fields(contact, back_template):
    """Called the moment the last of the 12 back fields has been resolved.
    If the chosen template doesn't comfortably fit how much the customer
    actually kept, offer switching to something roomier before handing off
    to the normal review/download prompt — never silently dropping a field
    either way, only ever adjusting compactness."""
    template = BACK_TEMPLATE_BY_ID.get(back_template)
    filled = count_filled_back_fields(contact)
    if template and filled > template["comfortable_field_count"]:
        suggestions = suggest_roomier_templates(back_template, filled)
        names = " or ".join(t["label"] for t in suggestions)
        reply = (
            f"You've added {filled} details — more than {template['label']} "
            f"usually shows well. Want to switch to something roomier like "
            f"{names}, or keep {template['label']} and fit everything in anyway?"
        )
        update = {
            "contact": contact,
            "pending_field": None,
            "pending_stage": None,
            "pending_value": None,
            "back_complete": True,
            "capacity_check_pending": True,
            "capacity_suggestions": [t["id"] for t in suggestions],
        }
        return reply, update

    reply = ("That's everything I need for the back! Want to review it, "
              "change anything, or download your card?")
    update = {
        "contact": contact,
        "pending_field": None,
        "pending_stage": None,
        "pending_value": None,
        "back_complete": True,
        "capacity_check_pending": False,
        "capacity_suggestions": [],
    }
    return reply, update


def clear_pending(contact, back_complete):
    return {
        "contact": contact,
        "pending_field": None,
        "pending_stage": None,
        "pending_value": None,
        "back_complete": back_complete,
        "capacity_check_pending": False,
    }


# ---------------------------------------------------------------------------
# QR CODE / BACK IMAGE SUB-FLOW — reached right after the customer answers
# "yes" to the qr_code field. Broadens the old plain yes/no into: upload an
# image, or have a real, scannable QR code generated from a URL. Its own
# small Python-owned gate sequence (deterministic — the vocabulary here is
# narrow enough that a keyword classifier is reliable, matching this
# project's pattern of never leaving a decision to open-ended guessing).
# ---------------------------------------------------------------------------

QR_SOURCE_QUESTION = (
    "Do you have an image ready to upload, or would you like me to "
    "generate a QR code from a URL, like your website?"
)
QR_URL_QUESTION = "What URL should the QR code link to?"

UPLOAD_IMAGE_WORDS = {
    "upload", "have one", "have an image", "i have one", "already have",
    "got one", "own image", "i have it", "have a file", "have a logo",
    "have an icon",
}
GENERATE_QR_WORDS = {
    "generate", "create", "make one", "make it", "url", "link",
    "generate one", "make a qr code", "qr code", "website",
}


def _clean_qr_url(text):
    """Very light deterministic clean-up: add a scheme if missing, and
    reject anything that clearly isn't a URL rather than guessing."""
    candidate = text.strip().strip("\"'\u201c\u201d\u2018\u2019 ")
    if not candidate:
        return None
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", candidate):
        candidate = "https://" + candidate.lstrip("/")
    domain_part = candidate.split("://", 1)[-1]
    if "." not in domain_part or " " in domain_part:
        return None
    return candidate


def start_qr_subflow(contact):
    update = {
        "contact": contact,
        "pending_field": "qr_code",
        "pending_stage": "qr_source",
        "pending_value": None,
        "back_complete": False,
        "capacity_check_pending": False,
    }
    return QR_SOURCE_QUESTION, update


def handle_qr_subflow(user_message, current_spec):
    contact = dict(current_spec.get("contact", {}) or {})
    stage = current_spec.get("pending_stage")
    back_template = current_spec.get("back_template")
    lower = user_message.lower().strip(" .!")

    def _stay(stage_name, reply_text):
        update = {
            "contact": contact,
            "pending_field": "qr_code",
            "pending_stage": stage_name,
            "pending_value": None,
            "back_complete": False,
            "capacity_check_pending": False,
        }
        return reply_text, update

    if stage == "qr_source":
        if lower in SKIP_WORDS or any(_starts_with_phrase(lower, w) for w in SKIP_WORDS):
            contact["qr_code"] = False
            return finish_or_advance(contact, back_template)
        if any(w in lower for w in UPLOAD_IMAGE_WORDS):
            return _stay("qr_awaiting_upload", "Great — go ahead and upload your image whenever you're ready.")
        if any(w in lower for w in GENERATE_QR_WORDS):
            return _stay("qr_url", QR_URL_QUESTION)
        return _stay("qr_source", QR_SOURCE_QUESTION)

    if stage == "qr_awaiting_upload":
        if contact.get("back_image"):
            return finish_or_advance(contact, back_template)
        return _stay("qr_awaiting_upload", "Whenever you're ready, use the upload button to add your image.")

    if stage == "qr_url":
        url = _clean_qr_url(user_message)
        if not url:
            return _stay("qr_url", "I didn't catch a web address there — what URL should the QR code link to?")
        contact["back_image"] = (
            "https://api.qrserver.com/v1/create-qr-code/?size=200x200&data="
            + urllib.parse.quote(url, safe="")
        )
        contact["back_image_kind"] = "qr"
        reply, update = finish_or_advance(contact, back_template)
        return f"Done — I generated a QR code linking to {url}.\n\n{reply}", update

    # Safety net — shouldn't normally be reached.
    return finish_or_advance(contact, back_template)


def handle_back_side_deterministic(user_message, current_spec):
    """Deterministic keyword-based fallback — used only if the LLM
    interpretation call fails (bad JSON, network error, timeout). Kept
    intact so the flow still works if the API has a hiccup."""
    contact = dict(current_spec.get("contact", {}) or {})
    for field in BACK_FIELDS:
        contact.setdefault(field["key"], None)

    pending_key = current_spec.get("pending_field")
    pending_stage = current_spec.get("pending_stage")
    pending_value = current_spec.get("pending_value")

    context_key = pending_key or (next_unanswered_field(contact) or {}).get("key")

    # A request to correct an already-answered field always takes priority,
    # even if we were mid-conversation about something else.
    edit_target = detect_edit_request(user_message, contact, context_key)
    if edit_target:
        return ask_for_value(edit_target, contact)

    if pending_key and pending_stage == "await_confirm":
        field = FIELD_BY_KEY[pending_key]
        intent = classify_confirmation(user_message)

        if intent == "yes":
            return commit_value(field, pending_value, contact, current_spec.get("back_template"))

        if intent == "no":
            # If they packed the correction into the same message
            # ("no, it's Coffee Shop"), use it immediately instead of
            # making them repeat themselves.
            if len(user_message.split()) > 2:
                extraction = extract_back_answer(user_message, field)
                return propose_confirmation(field, extraction, contact)
            return ask_for_value(field, contact)

        # Unclear reply — most likely they just typed the corrected value
        # directly without saying "no" first. Re-extract and re-confirm.
        extraction = extract_back_answer(user_message, field)
        return propose_confirmation(field, extraction, contact)

    if pending_key and pending_stage == "await_value":
        field = FIELD_BY_KEY[pending_key]
        extraction = extract_back_answer(user_message, field)
        return propose_confirmation(field, extraction, contact)

    target = next_unanswered_field(contact)
    if target is None:
        reply = ("Your back side has everything I need! Want to change "
                  "anything, or is it ready to download?")
        return reply, clear_pending(contact, back_complete=True)

    # The initial question is phrased as yes/no ("Do you want your full name
    # on the back?"). A bare "yes" answers that question — it isn't the
    # value itself — so ask for the actual value next instead of storing
    # the word "yes" as someone's name.
    if target["type"] == "text" and classify_confirmation(user_message) == "yes":
        return ask_for_value(target, contact)

    extraction = extract_back_answer(user_message, target)
    return propose_confirmation(target, extraction, contact)


# ---------------------------------------------------------------------------
# LLM-driven interpretation of back-side answers. Python still fully owns
# *state* (which field is being asked about, what order fields come in, what
# happens for each intent) — this only classifies what the customer meant,
# so unexpected phrasing, off-topic asides, or corrections don't break the
# flow the way keyword matching could.
# ---------------------------------------------------------------------------

BACK_INTENT_SYSTEM_PROMPT = """
You are the intent classifier for a business-card back-side detail
collector. You do NOT decide which question to ask next — a separate
system controls that. Your only job is to read the customer's most recent
message and classify what they meant, given the context you're provided.

Context you will receive as JSON:
- stage: "await_confirm" if we already extracted a value and are asking the
  customer to confirm it, otherwise "await_value"
- is_initial_yes_no_gate: true only when question_asked is phrased as a
  yes/no gate for a text field ("Do you want X on the back?") and no value
  has been requested from them yet
- field_key, field_label, field_type ("text" or "bool")
- question_asked: the exact question shown to the customer
- proposed_value: only present during "await_confirm" — the value we're
  asking them to confirm
- known_fields: object of field_key -> {label, value} for fields that
  already have a stored answer (for detecting "actually, change my X")

Respond with ONLY this JSON shape, nothing else:
{
  "intent": "provide_value" | "skip" | "confirm_yes" | "confirm_no" | "edit_other_field" | "off_topic",
  "value": "",
  "edit_field_key": null,
  "reply_to_user": ""
}

Rules:
- "provide_value": the customer supplied (or corrected) a real value for
  field_key. Put the CLEANED value in "value" (strip filler like "yeah it's",
  "my name is", surrounding quotes). For bool fields, put true/false.
- "skip": the customer doesn't want this field at all, in any wording
  ("no", "skip that", "not needed", "we're good without it", etc). Leave
  "value" empty/false.
- "confirm_yes" / "confirm_no": use ONLY when stage is "await_confirm" (are
  they agreeing/disagreeing with proposed_value) OR when
  is_initial_yes_no_gate is true and the customer gave a bare yes/no with no
  actual content (e.g. just "yes" — not "yes, John Smith", which is
  provide_value).
- "edit_other_field": the customer wants to go back and change a field
  OTHER than field_key that already appears in known_fields (e.g. "actually
  change my company name"). Set edit_field_key to that field's key.
- "off_topic": the customer asked a question, made small talk, or said
  something unrelated to answering field_key (e.g. "what's a QR code?",
  "do you do postcards too?", random chatter, confusion). Write a short,
  friendly "reply_to_user" that briefly addresses what they said, then ends
  by re-asking question_asked (verbatim or near-verbatim) so the
  conversation doesn't lose its place. Do not guess a value in this case.
- Never fabricate a value the customer didn't actually provide. If genuinely
  ambiguous, prefer "off_topic" with a clarifying reply_to_user over
  guessing.
"""


def _resolve_back_extraction(intent, value, field_type):
    """Turns an LLM intent + raw value into the {skip, value} shape the
    existing propose_confirmation/commit_value helpers expect."""
    if intent == "provide_value":
        if field_type == "bool":
            return {"skip": False, "value": bool(value)}
        return {"skip": False, "value": (value or "").strip() if isinstance(value, str) else str(value or "")}
    if intent == "skip":
        return {"skip": True, "value": False if field_type == "bool" else ""}
    if intent == "confirm_yes":
        if field_type == "bool":
            return {"skip": False, "value": True}
        return {"skip": False, "value": (value or "").strip() if isinstance(value, str) else str(value or "")}
    if intent == "confirm_no":
        return {"skip": True, "value": False if field_type == "bool" else ""}
    return None


def interpret_back_reply(user_message, contact, field, stage, pending_value, is_initial_yes_no_gate, question_asked):
    if not client:
        raise RuntimeError("GROQ_API_KEY is missing or empty")

    known_fields = {
        k: {"label": FIELD_BY_KEY[k]["label"], "value": v}
        for k, v in contact.items()
        if v is not None and k != field["key"] and k in FIELD_BY_KEY
    }

    context = {
        "stage": stage,
        "is_initial_yes_no_gate": is_initial_yes_no_gate,
        "field_key": field["key"],
        "field_label": field["label"],
        "field_type": field["type"],
        "question_asked": question_asked,
        "proposed_value": pending_value if stage == "await_confirm" else None,
        "known_fields": known_fields,
    }

    messages = [
        {"role": "system", "content": BACK_INTENT_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"Context: {json.dumps(context)}\n\nCustomer: {user_message}",
        },
    ]

    completion = client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=messages,
        response_format={"type": "json_object"},
        temperature=0.1,
        timeout=15,
    )

    raw = completion.choices[0].message.content.strip()
    result = json.loads(raw)

    if result.get("intent") not in {
        "provide_value", "skip", "confirm_yes", "confirm_no",
        "edit_other_field", "off_topic",
    }:
        raise ValueError("Model returned an unrecognized intent")

    return result


def handle_back_side(user_message, current_spec):
    contact = dict(current_spec.get("contact", {}) or {})
    for field in BACK_FIELDS:
        contact.setdefault(field["key"], None)

    pending_key = current_spec.get("pending_field")
    pending_stage = current_spec.get("pending_stage")
    pending_value = current_spec.get("pending_value")

    target = next_unanswered_field(contact)
    if pending_key is None and target is None:
        reply = ("Does this look like what you want, or would you like to "
                  "change anything before we send it off for printing?")
        return reply, clear_pending(contact, back_complete=True)

    field_key = pending_key or target["key"]
    field = FIELD_BY_KEY[field_key]
    stage = pending_stage or "await_value"
    is_initial_yes_no_gate = pending_key is None and field["type"] == "text"
    question_asked = (
        field["question"] if is_initial_yes_no_gate
        else (f"So {field['label']} will be: {pending_value}\n\nIs that right?"
              if stage == "await_confirm"
              else f"Sure — what should {field['label']} be?")
    )

    def _reprompt(reply_text):
        update = {
            "contact": contact,
            "pending_field": field_key,
            "pending_stage": stage,
            "pending_value": pending_value,
            "back_complete": next_unanswered_field(contact) is None,
        }
        return reply_text, update

    try:
        result = interpret_back_reply(
            user_message, contact, field, stage, pending_value,
            is_initial_yes_no_gate, question_asked,
        )
    except Exception as e:
        print(f"Back-side LLM interpretation failed, using fallback: {e}")
        return handle_back_side_deterministic(user_message, current_spec)

    intent = result.get("intent")

    if intent == "edit_other_field":
        edit_key = result.get("edit_field_key")
        if edit_key in FIELD_BY_KEY and contact.get(edit_key) is not None:
            return ask_for_value(FIELD_BY_KEY[edit_key], contact)
        intent = "off_topic"

    if intent == "off_topic":
        return _reprompt(result.get("reply_to_user") or question_asked)

    if stage == "await_confirm":
        if intent == "confirm_yes":
            return commit_value(field, pending_value, contact, current_spec.get("back_template"))
        if intent == "confirm_no":
            return ask_for_value(field, contact)
        extraction = _resolve_back_extraction(intent, result.get("value"), field["type"])
        if extraction is None:
            return _reprompt(question_asked)
        return propose_confirmation(field, extraction, contact)

    # stage == "await_value"
    if is_initial_yes_no_gate and intent == "confirm_yes":
        return ask_for_value(field, contact)
    extraction = _resolve_back_extraction(intent, result.get("value"), field["type"])
    if extraction is None:
        return _reprompt(question_asked)
    return propose_confirmation(field, extraction, contact)


def handle_front_side(user_message, current_spec, image_uploaded):
    # Deterministic safety net, checked BEFORE calling the model: a bare
    # short negative reply ("no", "nope", ...) once an image is already
    # uploaded and not declined is too high-stakes to leave entirely to a
    # small model's judgment — prompt-only guidance for this was tried and
    # still misfired in practice (the model kept reading "no" as "no image
    # at all" and reverting to text-only, discarding the uploaded image).
    # This mirrors the deterministic-fallback pattern used by every other
    # gate in this app. The reply is deliberately generic (doesn't presume
    # the pending question was specifically about size/position/full-bleed)
    # since we don't track exactly what was last asked here.
    if image_uploaded and not current_spec.get("image_declined") and _is_bare_negative(user_message):
        spec_update = dict(current_spec)
        spec_update["image_declined"] = False
        reply = (
            "No problem, I'll leave that as it is. Let me know anytime if "
            "you'd like to resize the image, reposition it, or make it "
            "full-bleed, or we can move on to the back whenever you're ready."
        )
        return reply, spec_update

    if not client:
        raise RuntimeError("GROQ_API_KEY is missing or empty")

    image_declined = bool(current_spec.get("image_declined", False))

    messages = [
        {"role": "system", "content": FRONT_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Image uploaded: {image_uploaded}\n"
                f"Image declined: {image_declined}\n"
                f"Current design_spec: {json.dumps(current_spec)}\n\n"
                f"Customer: {user_message}"
            ),
        },
    ]

    completion = client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=messages,
        response_format={"type": "json_object"},
        temperature=0.1,
        timeout=20,
    )

    raw = completion.choices[0].message.content.strip()
    response = json.loads(raw)

    if "reply_to_user" not in response or "design_spec" not in response:
        raise ValueError("Model returned incomplete JSON")

    return response["reply_to_user"], response["design_spec"]


# ---------------------------------------------------------------------------
# CARD SIDES GATE — the very first decision, before any front/back logic.
# A closed either/or question, answered via the LLM (with a deterministic
# keyword fallback) — this decides whether the customer gets a front side
# at all. Same pattern as the back-side interpreter: Python owns the
# question and the resulting state change, the LLM only classifies intent.
# ---------------------------------------------------------------------------

SIDES_GATE_QUESTION = "Would you like a one-sided or two-sided business card?"

IMAGE_GATE_QUESTION = (
    "Do you have an image you'd like to use, or would you like a text-only "
    "design instead?"
)

SIDES_INTENT_SYSTEM_PROMPT = """
You are an intent classifier for a single either/or question in a business
card design tool: "Would you like a one-sided or two-sided business card?"

Respond with ONLY this JSON shape, nothing else:
{
  "intent": "one" | "two" | "off_topic",
  "reply_to_user": ""
}

Rules:
- "one": the customer wants a one-sided card, in any wording ("just one
  side", "one is fine", "single-sided", "1", "one please").
- "two": the customer wants a two-sided card, in any wording ("two sides",
  "double-sided", "front and back", "2", "both sides").
- "off_topic": the customer asked a question, made small talk, or said
  something that doesn't answer the one/two question. Write a short,
  friendly "reply_to_user" that briefly addresses what they said, then ends
  by asking: "%s" (verbatim). Do not guess one vs two in this case.
""" % SIDES_GATE_QUESTION

ONE_SIDE_WORDS = {
    "one", "1", "one-sided", "one sided", "single", "single-sided",
    "single sided", "just one", "one side", "just the one",
}
TWO_SIDE_WORDS = {
    "two", "2", "two-sided", "two sided", "double", "double-sided",
    "double sided", "both", "both sides", "front and back", "two sides",
}


def _sides_fallback(user_message):
    """Deterministic fallback used only if the LLM call fails."""
    lower = user_message.lower().strip(" .!")
    if lower in ONE_SIDE_WORDS or any(_starts_with_phrase(lower, w) for w in ONE_SIDE_WORDS):
        return {"intent": "one", "reply_to_user": ""}
    if lower in TWO_SIDE_WORDS or any(_starts_with_phrase(lower, w) for w in TWO_SIDE_WORDS):
        return {"intent": "two", "reply_to_user": ""}
    return {"intent": "off_topic", "reply_to_user": SIDES_GATE_QUESTION}


def interpret_sides_reply(user_message):
    if not client:
        raise RuntimeError("GROQ_API_KEY is missing or empty")

    messages = [
        {"role": "system", "content": SIDES_INTENT_SYSTEM_PROMPT},
        {"role": "user", "content": f"Customer: {user_message}"},
    ]
    completion = client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=messages,
        response_format={"type": "json_object"},
        temperature=0.1,
        timeout=15,
    )
    raw = completion.choices[0].message.content.strip()
    result = json.loads(raw)
    if result.get("intent") not in {"one", "two", "off_topic"}:
        raise ValueError("Model returned an unrecognized intent")
    return result


def handle_sides_gate(user_message, current_spec):
    try:
        result = interpret_sides_reply(user_message)
    except Exception as e:
        print(f"Sides-gate LLM interpretation failed, using fallback: {e}")
        result = _sides_fallback(user_message)

    intent = result.get("intent")
    spec_update = dict(current_spec)

    if intent == "off_topic":
        spec_update["card_sides"] = None
        reply = result.get("reply_to_user") or SIDES_GATE_QUESTION
        return reply, spec_update

    if intent == "one":
        spec_update["card_sides"] = "one"
        contact = dict(current_spec.get("contact", {}) or {})
        for field in BACK_FIELDS:
            contact.setdefault(field["key"], None)
        spec_update["contact"] = contact
        spec_update["pending_field"] = None
        spec_update["pending_stage"] = None
        spec_update["pending_value"] = None
        spec_update["back_complete"] = False
        reply = (
            "Got it — one-sided it is. That means this one side is the "
            f"whole card, so let's fill in the details.\n\n{ORIENTATION_GATE_QUESTION_FRONT}"
        )
        return reply, spec_update

    # intent == "two"
    spec_update["card_sides"] = "two"
    reply = f"Two-sided it is. {ORIENTATION_GATE_QUESTION_FRONT}"
    return reply, spec_update


# ---------------------------------------------------------------------------
# ORIENTATION GATE — reached right after card_sides resolves, before the
# image question (two-sided) or before back-side Q&A (one-sided). Asked
# again for two-sided cards when the customer finishes the front and moves
# to the back, so front and back can differ. Same pattern as the other
# gates: Python owns the question/state, the LLM only classifies intent,
# anything ambiguous gets a clarifying re-ask instead of a guess.
# ---------------------------------------------------------------------------

ORIENTATION_GATE_QUESTION_FRONT = (
    "Would you like the front in landscape (wide) or portrait (tall) "
    "orientation?"
)
ORIENTATION_GATE_QUESTION_BACK = (
    "And for the back — landscape (wide) or portrait (tall)?"
)

ORIENTATION_INTENT_SYSTEM_PROMPT = """
You are an intent classifier for a single either/or question in a business
card design tool about card orientation: "landscape (wide) or portrait
(tall)?"

Respond with ONLY this JSON shape, nothing else:
{
  "intent": "landscape" | "portrait" | "off_topic",
  "reply_to_user": ""
}

Rules:
- "landscape": the customer wants a landscape/wide/horizontal orientation,
  in any wording ("landscape", "wide", "horizontal", "the normal way",
  "sideways").
- "portrait": the customer wants a portrait/tall/vertical orientation, in
  any wording ("portrait", "tall", "vertical", "upright", "standing up").
- "off_topic": the customer asked a question, made small talk, or said
  something that doesn't clearly answer landscape vs portrait. Write a
  short, friendly "reply_to_user" that briefly clarifies/answers what they
  said, then ends by asking the same orientation question verbatim as it
  was given to you. Never guess landscape/portrait here — if there's any
  doubt, use off_topic.
"""

LANDSCAPE_WORDS = {
    "landscape", "wide", "horizontal", "sideways", "the normal way", "wide way",
}
PORTRAIT_WORDS = {
    "portrait", "tall", "vertical", "upright", "standing up", "long way",
}


def _orientation_fallback(user_message):
    """Deterministic fallback used only if the LLM call fails."""
    lower = user_message.lower().strip(" .!")
    if lower in LANDSCAPE_WORDS or any(_starts_with_phrase(lower, w) for w in LANDSCAPE_WORDS):
        return {"intent": "landscape", "reply_to_user": ""}
    if lower in PORTRAIT_WORDS or any(_starts_with_phrase(lower, w) for w in PORTRAIT_WORDS):
        return {"intent": "portrait", "reply_to_user": ""}
    return {"intent": "off_topic", "reply_to_user": ""}


def interpret_orientation_reply(user_message, question_text):
    if not client:
        raise RuntimeError("GROQ_API_KEY is missing or empty")

    messages = [
        {"role": "system", "content": ORIENTATION_INTENT_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f'The question asked was: "{question_text}"\n'
                f"Customer: {user_message}"
            ),
        },
    ]
    completion = client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=messages,
        response_format={"type": "json_object"},
        temperature=0.1,
        timeout=15,
    )
    raw = completion.choices[0].message.content.strip()
    result = json.loads(raw)
    if result.get("intent") not in {"landscape", "portrait", "off_topic"}:
        raise ValueError("Model returned an unrecognized intent")
    return result


def handle_orientation_gate(user_message, current_spec, side):
    """side is "front" or "back" — decides which field gets set and which
    question re-asks on an unclear answer."""
    question_text = (
        ORIENTATION_GATE_QUESTION_FRONT if side == "front"
        else ORIENTATION_GATE_QUESTION_BACK
    )
    field_key = "front_orientation" if side == "front" else "back_orientation"

    try:
        result = interpret_orientation_reply(user_message, question_text)
    except Exception as e:
        print(f"Orientation-gate LLM interpretation failed, using fallback: {e}")
        result = _orientation_fallback(user_message)

    intent = result.get("intent")
    spec_update = dict(current_spec)

    if intent == "off_topic":
        spec_update[field_key] = None
        reply = result.get("reply_to_user") or question_text
        return reply, spec_update

    spec_update[field_key] = intent  # "landscape" or "portrait"

    if side == "front":
        if current_spec.get("card_sides") == "one":
            reply = f"Got it. {BACK_TEMPLATE_QUESTION}"
        else:
            reply = f"Got it. {IMAGE_GATE_QUESTION}"
    else:
        reply = f"Got it. {BACK_TEMPLATE_QUESTION}"

    return reply, spec_update


# ---------------------------------------------------------------------------
# IMAGE GATE — the second closed-ended decision, reached once card_sides ==
# "two". Same pattern as the sides gate: Python owns the question and the
# resulting state, the LLM only classifies intent, and anything ambiguous
# gets a clarifying re-ask instead of a guess. This is what fixes the
# "agent gives up and moves on" failure mode — a double-negative like "no,
# I don't want to skip it" was previously handled inside the big open-ended
# creative chat and got misread as declining the image.
# ---------------------------------------------------------------------------

IMAGE_INTENT_SYSTEM_PROMPT = """
You are an intent classifier for a single either/or question in a business
card design tool: "Do you have an image you'd like to use, or would you like
a text-only design instead?"

Respond with ONLY this JSON shape, nothing else:
{
  "intent": "has_image" | "no_image" | "off_topic",
  "reply_to_user": ""
}

Rules:
- "has_image": the customer has an image (logo, photo, graphic, etc.) and
  wants to use it, in any wording ("yes I have one", "I'll upload it", "I
  have a logo", "yeah", "of course" in response to being asked if they want
  to use an image).
  IMPORTANT — watch for double negatives: "no, I don't want to skip it"
  means they do NOT want to skip the image, i.e. they want to use it — that
  is "has_image", not "no_image". Read the whole sentence, don't just react
  to the first word.
- "no_image": the customer doesn't have an image, or wants to skip it and go
  text-only, in any wording ("no image", "no logo", "skip it", "text-only
  please", "don't have one").
- "off_topic": the customer asked a question, made small talk, expressed
  confusion, or said something that doesn't clearly answer the question
  (e.g. "do I have an image?", "what counts as an image", "does it
  matter?"). Write a short, friendly "reply_to_user" that briefly
  clarifies/answers what they said, then ends by asking: "Do you have an
  image you'd like to use, or would you like a text-only design instead?"
  (verbatim). Never guess has_image/no_image here — if there's any doubt,
  use off_topic.
"""

HAS_IMAGE_WORDS = {
    "yes", "yeah", "yep", "yup", "i have one", "i have a logo",
    "i have an image", "i'll upload it", "ill upload it", "sure", "of course",
}
NO_IMAGE_WORDS = {
    "no", "nope", "no logo", "no image", "skip it", "skip", "text only",
    "text-only", "dont have one", "don't have one", "i don't have one",
    "none",
}


def _image_fallback(user_message):
    """Deterministic fallback used only if the LLM call fails. Simple
    keyword matching can't reliably resolve double negatives, so anything
    containing an explicit negation word alongside "skip" is treated as
    off_topic rather than guessed at."""
    lower = user_message.lower().strip(" .!")
    if "skip" in lower and any(neg in lower for neg in ("don't", "dont", "not", "no ")):
        # e.g. "no I don't want to skip it" — too risky to guess, ask again.
        return {"intent": "off_topic", "reply_to_user": IMAGE_GATE_QUESTION}
    if lower in HAS_IMAGE_WORDS or any(_starts_with_phrase(lower, w) for w in HAS_IMAGE_WORDS):
        return {"intent": "has_image", "reply_to_user": ""}
    if lower in NO_IMAGE_WORDS or any(_starts_with_phrase(lower, w) for w in NO_IMAGE_WORDS):
        return {"intent": "no_image", "reply_to_user": ""}
    return {"intent": "off_topic", "reply_to_user": IMAGE_GATE_QUESTION}


def interpret_image_reply(user_message):
    if not client:
        raise RuntimeError("GROQ_API_KEY is missing or empty")

    messages = [
        {"role": "system", "content": IMAGE_INTENT_SYSTEM_PROMPT},
        {"role": "user", "content": f"Customer: {user_message}"},
    ]
    completion = client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=messages,
        response_format={"type": "json_object"},
        temperature=0.1,
        timeout=15,
    )
    raw = completion.choices[0].message.content.strip()
    result = json.loads(raw)
    if result.get("intent") not in {"has_image", "no_image", "off_topic"}:
        raise ValueError("Model returned an unrecognized intent")
    return result


def handle_image_gate(user_message, current_spec):
    try:
        result = interpret_image_reply(user_message)
    except Exception as e:
        print(f"Image-gate LLM interpretation failed, using fallback: {e}")
        result = _image_fallback(user_message)

    intent = result.get("intent")
    spec_update = dict(current_spec)

    if intent == "off_topic":
        spec_update["image_declined"] = None
        reply = result.get("reply_to_user") or IMAGE_GATE_QUESTION
        return reply, spec_update

    if intent == "has_image":
        spec_update["image_declined"] = False
        reply = "Great — go ahead and upload it whenever you're ready."
        return reply, spec_update

    # intent == "no_image" — hand off to the closed-ended content checklist
    # (business name / tagline / social-or-QR) instead of an open-ended
    # style question. The checklist's own reply already explains the plan
    # and asks its first question, so nothing more is needed here.
    spec_update["image_declined"] = True
    checklist_reply, checklist_update = start_front_checklist()
    spec_update.update(checklist_update)
    return checklist_reply, spec_update


# ---------------------------------------------------------------------------
# FRONT CONTENT CHECKLIST — reached once the customer has chosen a
# text-only front (image_declined == True). Same Python-owned, closed-ended
# pattern as BACK_FIELDS: every question is yes/no first (never an open
# "describe a style" prompt), and the next question is always computed here,
# never guessed by the model. Once complete, control hands off to the
# existing freeform FRONT_SYSTEM_PROMPT zone for style/size/color/placement
# adjustments only — content is already settled by then.
# ---------------------------------------------------------------------------

FRONT_BUSINESS_NAME_QUESTION = "Do you want your business name on the front?"
FRONT_BUSINESS_NAME_TEXT_QUESTION = "What's your business name?"
FRONT_TAGLINE_QUESTION = "Do you want a tagline or slogan under your business name?"
FRONT_TAGLINE_TEXT_QUESTION = "What should the tagline say?"
FRONT_EXTRA_QUESTION = (
    "Do you want social media handles or a QR code on the front too? "
    "Most cards keep this on the back, but it's your choice."
)
FRONT_EXTRA_CHOICE_QUESTION = "Would you like social media handles, or a QR code?"
FRONT_SOCIAL_TEXT_QUESTION = "What handle(s) would you like shown?"
FRONT_CHECKLIST_DONE_REPLY = (
    "Got it — here's how your front looks so far. Want to adjust the size, "
    "colors, or placement of anything, or is it good as is?"
)

SOCIAL_CHOICE_WORDS = {"social", "social media", "handles", "handle", "social handles", "social handle"}
QR_CHOICE_WORDS = {"qr", "qr code", "code", "a qr code", "qr-code"}


def start_front_checklist():
    """Called the moment image_declined becomes True (either from the
    initial image gate, or later if the customer decides mid-adjustment to
    drop an image and go text-only). Combines the plan explanation with the
    very first checklist question so there's no separate, wasted turn."""
    update = {
        "front_stage": "business_name_gate",
        "front_business_name_wanted": None,
        "front_business_name": None,
        "front_tagline_wanted": None,
        "front_tagline": None,
        "front_extra_wanted": None,
        "front_extra_type": None,
        "front_social": None,
        "front_qr_image": None,
        "front_layout": "Text-only front — collecting content",
    }
    reply = (
        "Got it, text-only front. A few quick questions, then we'll "
        "fine-tune the look. " + FRONT_BUSINESS_NAME_QUESTION
    )
    return reply, update


def handle_front_checklist(user_message, current_spec):
    spec = dict(current_spec)
    stage = spec.get("front_stage")
    lower = user_message.lower().strip(" .!")

    def is_yes(text):
        return text in YES_WORDS or any(_starts_with_phrase(text, w) for w in YES_WORDS)

    def is_no(text):
        return text in SKIP_WORDS or any(_starts_with_phrase(text, w) for w in SKIP_WORDS)

    if stage == "business_name_gate":
        if is_no(lower):
            spec["front_business_name_wanted"] = False
            spec["front_stage"] = "tagline_gate"
            return FRONT_TAGLINE_QUESTION, spec
        if is_yes(lower):
            spec["front_business_name_wanted"] = True
            spec["front_stage"] = "business_name_text"
            return FRONT_BUSINESS_NAME_TEXT_QUESTION, spec
        return FRONT_BUSINESS_NAME_QUESTION, spec

    if stage == "business_name_text":
        spec["front_business_name"] = user_message.strip()
        spec["front_stage"] = "tagline_gate"
        return FRONT_TAGLINE_QUESTION, spec

    if stage == "tagline_gate":
        if is_no(lower):
            spec["front_tagline_wanted"] = False
            spec["front_stage"] = "extra_gate"
            return FRONT_EXTRA_QUESTION, spec
        if is_yes(lower):
            spec["front_tagline_wanted"] = True
            spec["front_stage"] = "tagline_text"
            return FRONT_TAGLINE_TEXT_QUESTION, spec
        return FRONT_TAGLINE_QUESTION, spec

    if stage == "tagline_text":
        spec["front_tagline"] = user_message.strip()
        spec["front_stage"] = "extra_gate"
        return FRONT_EXTRA_QUESTION, spec

    if stage == "extra_gate":
        if is_no(lower):
            spec["front_extra_wanted"] = False
            spec["front_stage"] = "done"
            return FRONT_CHECKLIST_DONE_REPLY, spec
        if is_yes(lower):
            spec["front_extra_wanted"] = True
            spec["front_stage"] = "extra_choice"
            return FRONT_EXTRA_CHOICE_QUESTION, spec
        return FRONT_EXTRA_QUESTION, spec

    if stage == "extra_choice":
        if any(w in lower for w in QR_CHOICE_WORDS):
            spec["front_extra_type"] = "qr"
            spec["front_stage"] = "qr_source"
            return QR_SOURCE_QUESTION, spec
        if any(w in lower for w in SOCIAL_CHOICE_WORDS):
            spec["front_extra_type"] = "social"
            spec["front_stage"] = "social_text"
            return FRONT_SOCIAL_TEXT_QUESTION, spec
        return FRONT_EXTRA_CHOICE_QUESTION, spec

    if stage == "social_text":
        spec["front_social"] = user_message.strip()
        spec["front_stage"] = "done"
        return FRONT_CHECKLIST_DONE_REPLY, spec

    if stage == "qr_source":
        if any(w in lower for w in UPLOAD_IMAGE_WORDS):
            spec["front_stage"] = "qr_awaiting_upload"
            return "Great — go ahead and upload your image whenever you're ready.", spec
        if any(w in lower for w in GENERATE_QR_WORDS):
            spec["front_stage"] = "qr_url"
            return QR_URL_QUESTION, spec
        return QR_SOURCE_QUESTION, spec

    if stage == "qr_awaiting_upload":
        if spec.get("front_qr_image"):
            spec["front_stage"] = "done"
            return FRONT_CHECKLIST_DONE_REPLY, spec
        return "Whenever you're ready, use the upload button to add your image.", spec

    if stage == "qr_url":
        url = _clean_qr_url(user_message)
        if not url:
            return "I didn't catch a web address there — what URL should the QR code link to?", spec
        spec["front_qr_image"] = (
            "https://api.qrserver.com/v1/create-qr-code/?size=200x200&data="
            + urllib.parse.quote(url, safe="")
        )
        spec["front_stage"] = "done"
        reply = f"Done — I generated a QR code linking to {url}.\n\n{FRONT_CHECKLIST_DONE_REPLY}"
        return reply, spec

    # Safety net — shouldn't normally be reached (e.g. stale/legacy state
    # with no front_stage recorded). Restart the checklist from the top
    # rather than falling through to the freeform zone with no content.
    spec["front_stage"] = "business_name_gate"
    return FRONT_BUSINESS_NAME_QUESTION, spec




# ---------------------------------------------------------------------------
# BACK TEMPLATE GATE — asked once, right before the 12 field questions start.
# Same closed-ended pattern as every other gate: LLM classifies which of the
# 8 templates the customer meant (or off_topic), with a deterministic
# keyword + numeric fallback.
# ---------------------------------------------------------------------------

BACK_TEMPLATE_INTENT_SYSTEM_PROMPT = (
    "You are an intent classifier for a single question in a business card "
    "design tool asking the customer to choose a back-layout template from "
    "a fixed list.\n\nThe choices, in order, are:\n"
    + "\n".join(
        f"{i + 1}. {t['id']} - {t['label']}: {t['description']}"
        for i, t in enumerate(BACK_TEMPLATES)
    )
    + """

Respond with ONLY this JSON shape, nothing else:
{
  "intent": "<one of the ids above>" | "off_topic",
  "reply_to_user": ""
}

Rules:
- Match the customer's wording (including a bare number like "2" or "the
  second one", or a loose description like "the one with icons") to the
  closest listed id.
- "off_topic": the customer asked a question, made small talk, or said
  something that doesn't clearly pick one of the choices. Write a short,
  friendly "reply_to_user" that briefly responds, then re-ask the same
  question. Never guess a template here if there's real doubt.
"""
)

_BACK_TEMPLATE_IDS = {t["id"] for t in BACK_TEMPLATES}


def interpret_back_template_reply(user_message):
    if not client:
        raise RuntimeError("GROQ_API_KEY is missing or empty")

    messages = [
        {"role": "system", "content": BACK_TEMPLATE_INTENT_SYSTEM_PROMPT},
        {"role": "user", "content": f"Customer: {user_message}"},
    ]
    completion = client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=messages,
        response_format={"type": "json_object"},
        temperature=0.1,
        timeout=15,
    )
    raw = completion.choices[0].message.content.strip()
    result = json.loads(raw)
    if result.get("intent") not in _BACK_TEMPLATE_IDS | {"off_topic"}:
        raise ValueError("Model returned an unrecognized intent")
    return result


def _back_template_fallback(user_message):
    lower = user_message.lower().strip(" .!")
    if lower.isdigit():
        idx = int(lower) - 1
        if 0 <= idx < len(BACK_TEMPLATES):
            return {"intent": BACK_TEMPLATES[idx]["id"], "reply_to_user": ""}
    for t in BACK_TEMPLATES:
        if any(kw in lower for kw in t["keywords"]):
            return {"intent": t["id"], "reply_to_user": ""}
    return {"intent": "off_topic", "reply_to_user": BACK_TEMPLATE_QUESTION}


def handle_back_template_gate(user_message, current_spec):
    try:
        result = interpret_back_template_reply(user_message)
    except Exception as e:
        print(f"Back-template gate LLM interpretation failed, using fallback: {e}")
        result = _back_template_fallback(user_message)

    intent = result.get("intent")
    spec_update = dict(current_spec)

    if intent == "off_topic":
        reply = result.get("reply_to_user") or BACK_TEMPLATE_QUESTION
        return reply, spec_update

    spec_update["back_template"] = intent
    label = BACK_TEMPLATE_BY_ID[intent]["label"]
    # Normally this gate is reached once, before any field has been asked,
    # so BACK_FIELDS[0] is correct. But it can also be reached mid-checklist
    # (customer says "actually use a different template" partway through),
    # in which case resume at whatever field is still unanswered instead of
    # restarting the whole checklist — and clear any stale pending state
    # since we're re-asking that field's question fresh.
    contact = current_spec.get("contact") or {}
    next_field = next_unanswered_field(contact) or BACK_FIELDS[0]
    spec_update["pending_field"] = None
    spec_update["pending_stage"] = None
    spec_update["pending_value"] = None
    reply = f"Great choice — {label} it is.\n\n{next_field['question']}"
    return reply, spec_update


# ---------------------------------------------------------------------------
# BACK TEMPLATE CAPACITY CHECK — reached once, right after the last of the
# 12 field questions is resolved, only when the customer filled in more
# fields than the chosen template comfortably shows (see finish_back_fields).
# Deterministic keyword matching against the 1-2 suggested templates, plus a
# numeric shortcut and a set of "keep the current one" phrases.
# ---------------------------------------------------------------------------

KEEP_TEMPLATE_WORDS = {
    "keep", "stay", "fine as is", "leave it", "it's fine", "its fine",
    "no thanks", "no", "same", "current", "keep it", "keep this one",
}


def handle_back_template_capacity_check(user_message, current_spec):
    contact = dict(current_spec.get("contact", {}) or {})
    suggestion_ids = current_spec.get("capacity_suggestions") or []
    lower = user_message.lower().strip(" .!")
    spec_update = dict(current_spec)
    spec_update["contact"] = contact

    matched = None
    for tid in suggestion_ids:
        t = BACK_TEMPLATE_BY_ID.get(tid)
        if not t:
            continue
        if t["label"].lower() in lower or any(kw in lower for kw in t["keywords"]):
            matched = tid
            break
    if matched is None and lower.isdigit():
        idx = int(lower) - 1
        if 0 <= idx < len(suggestion_ids):
            matched = suggestion_ids[idx]

    if matched:
        spec_update["back_template"] = matched
        spec_update["capacity_check_pending"] = False
        spec_update["capacity_suggestions"] = []
        label = BACK_TEMPLATE_BY_ID[matched]["label"]
        reply = (
            f"Switched to {label}. Does this look like what you want, or "
            f"would you like to change anything before we send it off for printing?"
        )
        return reply, spec_update

    if any(w in lower for w in KEEP_TEMPLATE_WORDS):
        spec_update["capacity_check_pending"] = False
        spec_update["capacity_suggestions"] = []
        reply = ("Sounds good, keeping the current layout — everything will "
                  "still be shown, just a bit more compact. Does this look "
                  "like what you want, or would you like to change anything "
                  "before we send it off for printing?")
        return reply, spec_update

    # Unclear — re-ask the same closed question instead of guessing.
    template = BACK_TEMPLATE_BY_ID.get(current_spec.get("back_template"))
    template_label = template["label"] if template else "the current layout"
    names = " or ".join(
        BACK_TEMPLATE_BY_ID[t]["label"] for t in suggestion_ids if t in BACK_TEMPLATE_BY_ID
    )
    reply = (
        f"Just to confirm — want to switch to something roomier like "
        f"{names}, or keep {template_label} and fit everything in anyway?"
    )
    return reply, spec_update


# ---------------------------------------------------------------------------
# GLOBAL EDIT-REQUEST CHECK — runs first, before any stage-specific routing.
# Lets the customer change an already-resolved gate decision (sides,
# orientation, image) from anywhere in the conversation, not just while
# that question is actively being asked. This is what makes "actually,
# let's make it portrait instead" or "wait, I do have a logo after all"
# work mid-conversation instead of being stuck or misread. Back-side field
# edits and front-design tweaks already have their own mechanisms
# (edit_other_field / FRONT_SYSTEM_PROMPT) and are untouched by this check.
# ---------------------------------------------------------------------------

GLOBAL_EDIT_SYSTEM_PROMPT = """
You are watching a business-card design conversation for requests to change
a decision the customer already made earlier, from anywhere in the
conversation (not just whatever is currently being asked). You are given
the customer's current confirmed choices and their latest message.

Respond with ONLY this JSON shape, nothing else:
{
  "intent": "change_sides" | "change_orientation" | "change_image" | "change_back_template" | "none",
  "reply_to_user": ""
}

Rules:
- "change_sides": ONLY if card_sides is already decided (not null) AND the
  customer EXPLICITLY names the number of sides while asking to change it,
  in any wording ("actually let's do one-sided instead", "wait, can we make
  it two-sided", "switch to one side").
- "change_orientation": ONLY if an orientation is already decided (front or
  back is not null) AND the customer EXPLICITLY names landscape/portrait/
  orientation while asking to change it, in any wording ("actually make it
  portrait", "switch to landscape instead", "can we turn it sideways").
- "change_image": ONLY if image_declined is already decided (not null) AND
  the customer EXPLICITLY mentions the image/logo/photo while asking to
  add/remove/reconsider it, in any wording ("I do have a logo after all",
  "actually let's skip the image", "let's not use an image").
- "change_back_template": ONLY if back_template is already decided (not
  null) AND the customer EXPLICITLY asks to change the back layout/template,
  in any wording ("actually use a different layout", "can we change the
  template", "let's try the icon list style instead").
- "none": anything else — a normal answer to whatever's currently being
  asked, a back-side detail, small talk, or a front-design tweak that
  doesn't touch sides/orientation/image-yes-no/back_template. This should be
  the most common answer. If the field in question is still null (not yet
  decided), always answer "none" — that means it's a first-time answer to
  the current question, not a change request.
- IMPORTANT — vague requests with NO specific concept named: a bare "go
  back", "undo", "undo that", "undo it", "never mind", "revert", "revert
  that", or similar, WITHOUT explicitly naming sides/orientation/image/
  template/layout, must ALWAYS be "none". Do not guess which earlier
  decision they mean — only something else (already in progress elsewhere)
  handles those. Only classify as a change_* intent when the customer names
  the specific concept (e.g. "undo the two-sided thing", "go back to
  landscape", "undo the image", "undo the template" DO count; a bare "go
  back" or "undo" alone does NOT).
"""

CHANGE_TRIGGER_WORDS = (
    "actually", "instead", "change", "switch", "wait", "nevermind",
    "never mind", "i changed my mind", "can we make it", "let's make it",
    "lets make it", "let's do", "lets do", "reconsider", "undo", "revert",
    "go back to",
)


def _mentions_sides_concept(lower):
    return "side" in lower or any(_starts_with_phrase(lower, w) for w in ONE_SIDE_WORDS | TWO_SIDE_WORDS)


def _mentions_orientation_concept(lower):
    return "orientation" in lower or any(w in lower for w in LANDSCAPE_WORDS | PORTRAIT_WORDS)


def _mentions_image_concept(lower):
    return "image" in lower or "logo" in lower or "photo" in lower or "picture" in lower


def _mentions_back_template_concept(lower):
    return "template" in lower or "layout" in lower or any(
        kw in lower for t in BACK_TEMPLATES for kw in t["keywords"]
    )


def _validate_global_edit_intent(intent, user_message):
    """Deterministic guardrail applied to the LLM's OWN classification (not
    just its exception-fallback path below) — a change_* intent resets an
    already-answered decision, which is too high-stakes to trust from
    wording alone. Requires the message to literally mention the relevant
    concept, the same requirement _global_edit_fallback already used. This
    is what stops something like "an emoji smiley face" from being misread
    as "the customer wants to add a real image" (which used to reopen the
    already-answered image gate and derail the conversation) while still
    allowing genuine requests like "actually I do have a logo" through."""
    lower = user_message.lower().strip(" .!")
    if intent == "change_sides":
        return _mentions_sides_concept(lower)
    if intent == "change_orientation":
        return _mentions_orientation_concept(lower)
    if intent == "change_image":
        return _mentions_image_concept(lower)
    if intent == "change_back_template":
        return _mentions_back_template_concept(lower)
    return True  # "none" always passes through unchanged


def interpret_global_edit_request(user_message, current_spec):
    if not client:
        raise RuntimeError("GROQ_API_KEY is missing or empty")

    state_summary = (
        f"card_sides={current_spec.get('card_sides')}, "
        f"front_orientation={current_spec.get('front_orientation')}, "
        f"back_orientation={current_spec.get('back_orientation')}, "
        f"image_declined={current_spec.get('image_declined')}, "
        f"back_template={current_spec.get('back_template')}"
    )
    messages = [
        {"role": "system", "content": GLOBAL_EDIT_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"Current choices: {state_summary}\nCustomer: {user_message}",
        },
    ]
    completion = client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=messages,
        response_format={"type": "json_object"},
        temperature=0.1,
        timeout=15,
    )
    raw = completion.choices[0].message.content.strip()
    result = json.loads(raw)
    if result.get("intent") not in {
        "change_sides", "change_orientation", "change_image",
        "change_back_template", "none",
    }:
        raise ValueError("Model returned an unrecognized intent")
    if not _validate_global_edit_intent(result.get("intent"), user_message):
        # The model claimed a change_* intent but the message doesn't
        # literally mention the concept it claims to be changing — treat
        # as a normal (non-edit) message instead of trusting the guess.
        result = {"intent": "none", "reply_to_user": result.get("reply_to_user", "")}
    return result


def _global_edit_fallback(user_message, current_spec):
    """Deterministic fallback used only if the LLM call fails. Requires an
    explicit change-signal word AND a mention of the relevant concept AND
    that field already being resolved — otherwise a normal first-time
    answer like "two-sided" or "portrait" could be misread as an edit
    request instead of an answer to the question actually being asked."""
    lower = user_message.lower().strip(" .!")
    if not any(w in lower for w in CHANGE_TRIGGER_WORDS):
        return {"intent": "none"}

    if current_spec.get("card_sides") is not None and _mentions_sides_concept(lower):
        return {"intent": "change_sides"}

    if (
        current_spec.get("front_orientation") is not None
        or current_spec.get("back_orientation") is not None
    ) and _mentions_orientation_concept(lower):
        return {"intent": "change_orientation"}

    if current_spec.get("image_declined") is not None and _mentions_image_concept(lower):
        return {"intent": "change_image"}

    if current_spec.get("back_template") is not None and _mentions_back_template_concept(lower):
        return {"intent": "change_back_template"}

    return {"intent": "none"}


def route_conversation(user_message, current_spec, front_locked, image_uploaded):
    """The single source of truth for "what question comes next" given the
    current state. Used both for normal turns and for resuming after a
    global edit request resets one of the gate fields."""
    card_sides = current_spec.get("card_sides")
    front_orientation = current_spec.get("front_orientation")
    back_orientation = current_spec.get("back_orientation")
    image_declined = current_spec.get("image_declined")

    if card_sides is None:
        return handle_sides_gate(user_message, current_spec)

    if card_sides == "one":
        if front_orientation is None:
            return handle_orientation_gate(user_message, current_spec, "front")
        return route_back_flow(user_message, current_spec)

    # card_sides == "two"
    if front_orientation is None:
        return handle_orientation_gate(user_message, current_spec, "front")
    if front_locked:
        if back_orientation is None:
            return handle_orientation_gate(user_message, current_spec, "back")
        return route_back_flow(user_message, current_spec)
    if image_declined is None:
        return handle_image_gate(user_message, current_spec)
    if image_declined and current_spec.get("front_stage") != "done":
        return handle_front_checklist(user_message, current_spec)
    return handle_front_side(user_message, current_spec, image_uploaded)


def route_back_flow(user_message, current_spec):
    """Once orientation is settled for the back side, this decides between
    the template picker, the QR/image sub-flow, the capacity check, and the
    normal field-by-field checklist — in that priority order."""
    if current_spec.get("back_template") is None:
        return handle_back_template_gate(user_message, current_spec)
    if current_spec.get("pending_stage") in ("qr_source", "qr_awaiting_upload", "qr_url"):
        return handle_qr_subflow(user_message, current_spec)
    if current_spec.get("capacity_check_pending"):
        return handle_back_template_capacity_check(user_message, current_spec)
    return handle_back_side(user_message, current_spec)


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/api/chat":
            self.send_error(404)
            return

        current_spec = {}
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            data = json.loads(body.decode("utf-8"))

            user_message = data.get("message", "").strip()
            current_spec = data.get("state", {}) or {}
            image_uploaded = data.get("image_uploaded", False)
            front_locked = data.get("front_locked", False)
            card_sides = current_spec.get("card_sides")
            image_declined = current_spec.get("image_declined")

            print(f"\n--- New request ---")
            print(f"card_sides: {card_sides}, image_declined: {image_declined}, front_locked: {front_locked}")
            print(f"User: {user_message}")

            # Global edit-request check — only meaningful once at least the
            # sides gate has resolved (nothing to "change" before that).
            edit_intent = "none"
            if card_sides is not None:
                try:
                    global_result = interpret_global_edit_request(user_message, current_spec)
                except Exception as e:
                    print(f"Global edit-request check failed, using fallback: {e}")
                    global_result = _global_edit_fallback(user_message, current_spec)
                edit_intent = global_result.get("intent", "none")

            spec_for_routing = dict(current_spec)
            routing_front_locked = front_locked
            front_locked_override = None
            prefix = None

            if edit_intent == "change_sides" and spec_for_routing.get("card_sides") is not None:
                spec_for_routing["card_sides"] = None
                spec_for_routing["front_orientation"] = None
                spec_for_routing["back_orientation"] = None
                spec_for_routing["image_declined"] = None
                routing_front_locked = False
                front_locked_override = False
                prefix = "No problem, let's redo that. "
            elif edit_intent == "change_orientation" and (
                spec_for_routing.get("front_orientation") is not None
                or spec_for_routing.get("back_orientation") is not None
            ):
                if routing_front_locked:
                    spec_for_routing["back_orientation"] = None
                else:
                    spec_for_routing["front_orientation"] = None
                prefix = "Sure, let's change that. "
            elif edit_intent == "change_image" and spec_for_routing.get("image_declined") is not None:
                spec_for_routing["image_declined"] = None
                if routing_front_locked:
                    routing_front_locked = False
                    front_locked_override = False
                prefix = "No problem, let's revisit that. "
            elif edit_intent == "change_back_template" and spec_for_routing.get("back_template") is not None:
                spec_for_routing["back_template"] = None
                spec_for_routing["capacity_check_pending"] = False
                spec_for_routing["capacity_suggestions"] = []
                prefix = "Sure, let's pick a different layout. "

            if prefix is not None:
                # Re-run routing on the reset state using the customer's
                # actual message (not an empty string). If they already
                # named the new value in their edit request (e.g. "change
                # to landscape"), the freshly-reopened gate's own
                # classifier will resolve it directly in this same turn.
                # If they only named the concept without a value (e.g.
                # "let's change the orientation"), that same classifier
                # correctly falls back to its normal off-topic re-ask.
                # (Passing "" here used to force the gate to guess from
                # nothing, which is unreliable and caused garbled replies.)
                reply, spec_update = route_conversation(user_message, spec_for_routing, routing_front_locked, image_uploaded)
                reply = prefix + reply
            else:
                reply, spec_update = route_conversation(user_message, current_spec, front_locked, image_uploaded)
                routing_front_locked = front_locked

            # front_confirmed is a one-turn signal from the front-side LLM
            # (handle_front_side) meaning "the customer is happy with the
            # front and ready to move on" — recognized in any wording, not
            # just the fixed handful of phrases the client checks locally
            # before ever calling the server. front_confirmed itself is
            # transient (not a persisted design field), so it's read here
            # and then stripped before the response is built.
            front_confirmed = bool(spec_update.pop("front_confirmed", False))

            # If handle_front_side (the freeform adjustment zone, only
            # reachable once image_declined is False or the text-only
            # checklist is already "done") just flipped image_declined from
            # False to True — i.e. the customer decided mid-adjustment to
            # drop the image and go text-only after all — kick off the same
            # Python-owned content checklist used when text-only is chosen
            # from the very start, instead of leaving them with no business
            # name/tagline/social content collected at all.
            if (
                image_declined is False
                and spec_update.get("image_declined") is True
                and not spec_update.get("front_stage")
            ):
                checklist_reply, checklist_update = start_front_checklist()
                spec_update.update(checklist_update)
                reply = checklist_reply

            # Tell the frontend whether to show the visual orientation
            # picker, back-template picker, or capacity-check suggestions
            # for whatever question is about to be asked. spec_update from
            # the back-field/QR handlers is often a partial dict (it doesn't
            # repeat card_sides/orientation/back_template), so merge it onto
            # the full previous state first — current_spec always has the
            # complete picture since the client sends its full design_spec
            # on every request.
            merged_spec = {**current_spec, **spec_update}

            # When front_confirmed just came in, lock the front here via the
            # existing front_locked_override mechanism and hand off to the
            # back exactly like the client's own local keyword shortcut
            # already does for the phrases it recognizes — this is the
            # server-side safety net that catches every other phrasing.
            if front_confirmed and not routing_front_locked and merged_spec.get("card_sides") == "two":
                routing_front_locked = True
                front_locked_override = True
                if merged_spec.get("back_orientation") is None:
                    next_question = ORIENTATION_GATE_QUESTION_BACK
                elif merged_spec.get("back_template") is None:
                    next_question = BACK_TEMPLATE_QUESTION
                else:
                    next_field = next_unanswered_field(merged_spec.get("contact", {}) or {})
                    next_question = next_field["question"] if next_field else BACK_TEMPLATE_QUESTION
                reply = "Front is locked. Now we build the back.\n\n" + next_question

            awaiting_gate = None
            if merged_spec.get("card_sides") is not None:
                if merged_spec.get("front_orientation") is None:
                    awaiting_gate = "orientation_front"
                elif (
                    merged_spec.get("card_sides") == "two"
                    and routing_front_locked
                    and merged_spec.get("back_orientation") is None
                ):
                    awaiting_gate = "orientation_back"
                else:
                    if merged_spec.get("front_stage") == "qr_awaiting_upload":
                        awaiting_gate = "front_qr_image_upload"
                    back_stage_reached = (
                        merged_spec.get("card_sides") == "one"
                        or (merged_spec.get("card_sides") == "two" and routing_front_locked)
                    )
                    if back_stage_reached and awaiting_gate is None:
                        if merged_spec.get("back_template") is None:
                            awaiting_gate = "back_template"
                        elif merged_spec.get("capacity_check_pending"):
                            awaiting_gate = "back_template_capacity"
                        elif merged_spec.get("pending_stage") == "qr_awaiting_upload":
                            awaiting_gate = "qr_image_upload"

            response = {"reply_to_user": reply, "design_spec": spec_update, "awaiting_gate": awaiting_gate}
            if front_locked_override is not None:
                response["front_locked_override"] = front_locked_override

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode("utf-8"))

        except Exception as e:
            print("\n========== ERROR ==========")
            traceback.print_exc()
            print("===========================\n")

            error = {
                "reply_to_user": f"Error: {str(e)}",
                "design_spec": current_spec,
            }
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(error).encode("utf-8"))

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


class ThreadingHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)) or ".")
    with ThreadingHTTPServer(("", PORT), Handler) as httpd:
        print(f"Server running -> http://localhost:{PORT}")
        httpd.serve_forever()

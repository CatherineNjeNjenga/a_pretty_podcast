"""Rule-based tagging of who/what a YouTube comment is about. No libraries beyond the standard one + numpy.

Categories (first match wins, in this order):
  Noise     link/handle only, timestamps only, "1st/first/top", self-promotion
  Request   asks for a guest or collaboration (English + Russian) - wins over any name in the comment,
            so "Мария, пригласи X" is NOT counted as engagement with Maria
  Maria / Guest / Both   names her (Latin, Cyrillic, Chinese) / the guest (aliases) / both
  Show      judges the show or episode (cue words, or praise aimed at "this")
  Pair      about both hosts without names ("two queens", "great chemistry")
  Unnamed   she/her or a role (host, presenter, interviewer) with no name - reported separately, never in the gap
  Reaction  emoji/symbol-only or a short generic feeling ("Congratulations!", "Wow")
  Topic     readable comment about the subject (remainder, 5+ words)
  Neither   anything else (very short, unreadable)

Matching is done on normalised text: accents removed, lower case, stretched letters collapsed
("Lindseeeeeeyyyy" -> "lindsey"), and "#MariaSharapova" still matches (sharapova is matched inside longer words). Nothing here uses the tone score.
"""
import re
import unicodedata

import numpy as np


def _strip(t):
    t = unicodedata.normalize("NFKD", t)
    return "".join(c for c in t if not unicodedata.combining(c)).lower()


def variants(t):
    """The text as written, plus two ways of shrinking stretched letters (3+ in a row -> 1, or -> 2)."""
    s = _strip(t)
    return list(dict.fromkeys([s, re.sub(r"(.)\1{2,}", r"\1", s), re.sub(r"(.)\1{2,}", r"\1\1", s)]))


MARIA_RX = re.compile(r"\b(maria|masha)\b|sharapova|\b\u043c\u0430\u0440\u0438[\u044f\u0438\u044e\u0435]\b|\b\u043c\u0430\u0448[\u0430\u0438\u0443\u0435\u043e]\w*|\b\u0448\u0430\u0440\u0430\u043f\u043e\u0432\w*|\u838e\u62c9\u6ce2\u5a03")


def alias_regex(aliases):
    parts = [re.escape(_strip(a).strip()) for a in aliases.split("|") if a.strip()]
    parts = [p for p in parts if p and not MARIA_RX.fullmatch(p)]   # a guest alias must not be Maria's name
    return re.compile(r"\b(" + "|".join(parts) + r")\b") if parts else None


def _any(rx, vs):
    return any(rx.search(v) for v in vs)


URL_RX = re.compile(r"https?://\S+|www\.\S+|\byoutu\.be/\S+|\S+\.(com|ru|net)/\S*")
PROMO_RX = re.compile(r"subscribe to (me|my)|my channel|check (out )?my|follow me|\bdm me\b|"
                      r"\u043f\u043e\u0434\u043f\u0438\u0448\u0438\u0441\u044c \u043d\u0430 (\u043c\u0435\u043d\u044f|\u043c\u043e\u0439)|\u043c\u043e\u0439 \u043a\u0430\u043d\u0430\u043b")
FIRST_RX = re.compile(r"^\W*(1st|first|early|top|top 1st|1st top)\W*$")
REQUEST_RX = re.compile(
    r"\binvit\w*|\bplease\b.{0,25}\b(have|bring|get|interview|next)\b|\b(bring|get|have)\b.{1,30}\bon (your |the )?(podcast|show)\b|"
    r"\bnext guest\b|\bnext (episode|video)\b|^\W*next\b|\bwe need\b.{0,30}\b(on|interview|podcast)\b|\bneed\b.{1,25}\b(podcast|interview)\b|"
    r"\bwhen (are|will) you\b|\bwould love (it )?if you\b|\bwould be cool to see\b|\bhope to see you (collab|interview)\w*|"
    r"\binterview with\b|\u043f\u0440\u0438\u0433\u043b\u0430\u0441\w*|\u043f\u0440\u0438\u0433\u043b\u0430\u0441\u0438\w*|\u043f\u043e\u0437\u043e\u0432\w*|"
    r"\u043e\u0440\u0433\u0430\u043d\u0438\u0437\u0443\u0438|\u0441\u0445\u043e\u0434\u0438|\u0445\u043e\u0447\u0443 \u0443\u0432\u0438\u0434\u0435\u0442\u044c|\u0436\u0434\u0435\u043c")
SHOW_RX = re.compile(
    r"\b(shows?|podcasts?|episodes?|interviews?|interviewed|conversations?|series|channel|youtube|videos?|subscri\w+|listen\w*|"
    r"watch\w*|advertis\w+|ads|intro|infotainment|content|production|audio|editing|couch|discussions?|enjoy(ed|able)?|"
    r"much.awaited|so much fun|so fun|looking forward to this)\b|thank\w*.{0,15}\b(creating|making|sharing|doing|bringing)\b|"
    r"\b(love[d]?|enjoy(ed|able)?|great|amazing|good|fun|helpful|wholesome|interesting|inspiring|informative)\b.{0,20}\b(this|these)\b|"
    r"\b(this|these)\b.{0,25}\b(love|enjoy\w*|wholesome|fun|helpful|great|amazing|inspiring|informative|good)\b|"
    r"\u0438\u043d\u0442\u0435\u0440\u0432\u044c\u044e|\u043f\u043e\u0434\u043a\u0430\u0441\u0442|\u043a\u0430\u043d\u0430\u043b|\u0432\u0438\u0434\u0435\u043e|\u0432\u044b\u043f\u0443\u0441\u043a|\u043f\u0435\u0440\u0435\u0434\u0430\u0447\w*|\u0448\u043e\u0443")
PAIR_RX = re.compile(r"\b(two|2)\b.{0,12}\b(queens?|legends?|goddess\w*|icons?|ladies|women)\b|"
                     r"\b(you two|you both|both of you|you guys|the two of you|them both|chemistry)\b|\bthank (you|u) ladies\b|\blove you both\b")
UNNAMED_RX = re.compile(r"\b(she|her|hers|she's|herself|host|presenter|interviewer)\b")
REACT_RX = re.compile(
    r"\b(congrat\w*|wow|amazing|awesome|excellent|nice|great|good|love\w*|yes+|beautiful|perfect|best|queen|legend|goddess\w*|"
    r"thank\w*|lovely|fire|brilliant|incredible|crazy|major|silent|cool|fun|gorgeous|yay|omg)\b|"
    r"\u043f\u043e\u0437\u0434\u0440\u0430\u0432\u043b\u044f\w*|\u0441\u0443\u043f\u0435\u0440|\u043a\u043b\u0430\u0441\u0441|\u043a\u0440\u0443\u0442\u043e|\u043c\u043e\u043b\u043e\u0434\u0435\u0446|"
    r"\u0441\u043f\u0430\u0441\u0438\u0431\u043e|\u043b\u044e\u0431\u043b\u044e|\u043e\u0431\u043e\u0436\u0430\u044e|\u043a\u0440\u0430\u0441\u0438\u0432\w*")

CATEGORIES = ["Maria", "Guest", "Both", "Show", "Request", "Pair", "Unnamed", "Reaction", "Topic", "Noise", "Neither"]


def _one(text, grx):
    vs = variants(text)
    stripped = URL_RX.sub(" ", vs[0])
    words = re.findall(r"\w+", stripped)
    if not any(ch.isalnum() for ch in vs[0]):
        return "Reaction"                                   # emoji / symbols only
    if URL_RX.search(vs[0]) and len(words) <= 3:
        return "Noise"
    if re.fullmatch(r"[\d:\s.,\-]+", vs[0].strip()) or FIRST_RX.match(vs[0].strip()) or PROMO_RX.search(vs[0]):
        return "Noise"
    if _any(REQUEST_RX, vs):
        return "Request"
    m = _any(MARIA_RX, vs)
    g = bool(grx) and _any(grx, vs)
    if m and g:
        return "Both"
    if m:
        return "Maria"
    if g:
        return "Guest"
    if _any(SHOW_RX, vs):
        return "Show"
    if _any(PAIR_RX, vs):
        return "Pair"
    if _any(UNNAMED_RX, vs):
        return "Unnamed"
    if len(words) <= 6 and _any(REACT_RX, vs):
        return "Reaction"
    return "Topic" if len(words) >= 5 else "Neither"


def classify(df):
    """df needs columns: video_id, guest_aliases (pipe-separated), comment. Returns a numpy array of categories."""
    rx = {v: alias_regex(a) for v, a in df.drop_duplicates("video_id")[["video_id", "guest_aliases"]].values}
    return np.array([_one(t, rx[v]) for v, t in zip(df["video_id"], df["comment"])])

"""Rule-based tagging of who/what a YouTube comment is about. No libraries beyond the standard one + numpy.

Categories (first match wins, in this order):
  Noise     link/handle only, timestamps only, "1st/first/top", self-promotion
  Request   asks for a guest or collaboration (English + Russian) - wins over any name in the comment,
            so "Мария, пригласи X" is NOT counted as engagement with Maria
  Maria / Guest / Both   names her (Latin, Cyrillic, Chinese) / the guest (aliases) / both
  Work      no name, but mentions Maria's or the guest's business/book/organisation (work_terms.csv), e.g. "Sugarpova", "Lakers";
            which side it belongs to is stored in the separate `work` flag. Never counted in the gap or in Both.
  Show      judges the show or episode (cue words, or praise aimed at "this")
  Pair      about both hosts without names ("two queens", "great chemistry")
  Unnamed   she/her with no name (leans toward the guest) - reported separately, never in the gap
  Host      the words host / presenter / interviewer with no name and no she/her (leans toward Maria) - never in the gap
  Reaction  emoji/symbol-only or a short generic feeling ("Congratulations!", "Wow")
  Topic     readable comment about the subject (remainder, 5+ words)
  Neither   anything else (very short, unreadable)

Matching is done on normalised text: accents removed, lower case, stretched letters collapsed
("Lindseeeeeeyyyy" -> "lindsey"), and "#MariaSharapova" still matches (sharapova is matched inside longer words). Nothing here uses the tone score.
"""
import csv
import os
import re
import unicodedata

import numpy as np

BUILD = "2026-10-10.5"   # bumped with every release; run_weekly.py checks that all the files below carry the same value


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

# --- Work terms (work_terms.csv) -------------------------------------------------------------------------------
# scope,guest,term,kind,risky,on,note.  Rows with on=no are ignored.
#  * kind=topic rows (scope maria) are the separate topic list (doping): they set the `topic` flag, never the tag.
#  * every other row is a Work term: it sets the `work` flag (Maria / Guest / Both) on ANY comment containing it, and a
#    comment with no name in it gets the tag "Work" instead of falling through to Show/Topic/Neither.
#  * risky=yes means the word is also ordinary chatter ("lakers", "wta", "vogue"): it only sets the flag when the comment also
#    names Maria or the guest, and never creates the "Work" tag on its own. Topic terms that are risky need the comment to be
#    tagged Maria or Both (i.e. about her).
#  * guest-scope terms apply only in that guest's own episode.
WORK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "work_terms.csv")
ALSO = {"nobel peace price": ["nobel peace prize"], "j crew": ["jcrew", "j.crew"], "gabriela hearst": ["gabi hearst"]}
_WORK = None


def _csv_rows(path):
    """Rows of a small hand-edited CSV. Tries UTF-8 then Windows-1252 (what Excel's plain "CSV" save uses). A file that cannot be read
    at all is reported in the log and treated as empty, so a bad edit never stops the weekly run."""
    for enc in ("utf-8-sig", "cp1252"):
        try:
            with open(path, encoding=enc, newline="") as f:
                return list(csv.DictReader(f))
        except UnicodeDecodeError:
            continue
        except Exception as ex:
            print(f"WARNING: could not read {os.path.basename(path)} ({ex}); ignoring it this run")
            return []
    print(f"WARNING: could not decode {os.path.basename(path)}; ignoring it this run (save it as CSV UTF-8)")
    return []


def _term_rx(term):
    forms = [term] + ALSO.get(_strip(term).strip(), [])
    return re.compile(r"(?<!\w)(" + "|".join(re.escape(_strip(f).strip()) for f in forms) + r")(?!\w)")


def work_terms(path=None):
    """{'maria': [(rx, risky, kind, term)], 'guests': {guest_key: [(rx, risky, kind, term)]}} from work_terms.csv (cached)."""
    global _WORK
    if _WORK is not None and path is None:
        return _WORK
    out = {"maria": [], "guests": {}}
    p = path or WORK_FILE
    if os.path.exists(p):
        for r in _csv_rows(p):
            term = (r.get("term") or "").strip()
            if not term or (r.get("on") or "yes").strip().lower() != "yes":
                continue
            item = (_term_rx(term), (r.get("risky") or "").strip().lower() == "yes", (r.get("kind") or "").strip().lower(), term.lower())
            if (r.get("scope") or "").strip().lower() == "maria":
                out["maria"].append(item)
            else:
                out["guests"].setdefault(_strip(r.get("guest") or "").strip(), []).append(item)
    if path is None:
        _WORK = out
    return out


def guests_without_terms(guests):
    """Guest names (from episodes) that have no active Work-term rows in work_terms.csv, in the order given."""
    have = work_terms()["guests"]
    return [g for g in dict.fromkeys(guests) if g and _strip(g).strip() not in have]


def _work_flags(vs, about, guest_key):
    """(work side or None, True if a non-risky Work term matched, topic flag or None)."""
    wt = work_terms()
    named = about in ("Maria", "Guest", "Both") or None
    sides, safe, topic = set(), False, None
    for side, items in (("Maria", wt["maria"]), ("Guest", wt["guests"].get(guest_key, []))):
        for rx, risky, kind, _ in items:
            if not _any(rx, vs):
                continue
            if kind == "topic":
                if side == "Maria" and (not risky or about in ("Maria", "Both")):
                    topic = "doping"
                continue
            if risky and not named:
                continue
            sides.add(side)
            safe = safe or not risky
    work = None if not sides else ("Both" if len(sides) == 2 else sides.pop())
    return work, safe, topic


URL_RX = re.compile(r"https?://\S+|www\.\S+|\byoutu\.be/\S+|\S+\.(com|ru|net)/\S*")
PROMO_RX = re.compile(r"subscribe to (me|my)|my channel|check (out )?my|follow me|\bdm me\b|"
                      r"\u043f\u043e\u0434\u043f\u0438\u0448\u0438\u0441\u044c \u043d\u0430 (\u043c\u0435\u043d\u044f|\u043c\u043e\u0439)|\u043c\u043e\u0439 \u043a\u0430\u043d\u0430\u043b")
FIRST_RX = re.compile(r"^\W*(1st|first|early|top|top 1st|1st top)\W*$")
REQUEST_RX = re.compile(
    r"\binvit\w*|\bplease\b.{0,25}\b(have|bring|get|interview|next)\b|\b(bring|get|have)\b.{1,30}\bon (your |the )?(podcast|show)\b|"
    r"\bnext guest\b|\bnext\W*(pls|plz|please)?\W*$|^\W*next (episode|video)\b|\bnext (episode|video)\W{0,3}(with|featuring|ft|guest|please|pls|plz)\b|"
    r"\b(have|bring|invite|get|want|wish|hope|wanna|make|do)\b.{0,40}\bnext (episode|video)\b|^\W*next\b|\bwe need\b.{0,30}\b(on|interview|podcast)\b|\bneed\b.{1,25}\b(podcast|interview)\b|"
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
UNNAMED_RX = re.compile(r"\b(she|her|hers|she's|herself)\b")
HOSTWORD_RX = re.compile(r"\b(host|presenter|interviewer)\b")
REACT_RX = re.compile(
    r"\b(congrat\w*|wow|amazing|awesome|excellent|nice|great|good|love\w*|yes+|beautiful|perfect|best|queen|legend|goddess\w*|"
    r"thank\w*|lovely|fire|brilliant|incredible|crazy|major|silent|cool|fun|gorgeous|yay|omg)\b|"
    r"\u043f\u043e\u0437\u0434\u0440\u0430\u0432\u043b\u044f\w*|\u0441\u0443\u043f\u0435\u0440|\u043a\u043b\u0430\u0441\u0441|\u043a\u0440\u0443\u0442\u043e|\u043c\u043e\u043b\u043e\u0434\u0435\u0446|"
    r"\u0441\u043f\u0430\u0441\u0438\u0431\u043e|\u043b\u044e\u0431\u043b\u044e|\u043e\u0431\u043e\u0436\u0430\u044e|\u043a\u0440\u0430\u0441\u0438\u0432\w*")

# Bump this whenever a rule, word list or alias logic below changes in a way that can change a tag. It is stored with
# every comment so you can tell which rules produced each tag (older comments whose text is purged keep their old version).
RULES_VERSION = "2026-10-10.4"

CATEGORIES = ["Maria", "Guest", "Both", "Work", "Show", "Request", "Pair", "Unnamed", "Host", "Reaction", "Topic", "Noise", "Neither"]


def _one(text, grx, gkey=""):
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
    if _work_flags(vs, "", gkey)[1]:
        return "Work"
    if _any(SHOW_RX, vs):
        return "Show"
    if _any(PAIR_RX, vs):
        return "Pair"
    if _any(UNNAMED_RX, vs):
        return "Unnamed"
    if _any(HOSTWORD_RX, vs):
        return "Host"
    if len(words) <= 6 and _any(REACT_RX, vs):
        return "Reaction"
    return "Topic" if len(words) >= 5 else "Neither"


def script_of(text):
    """Writing system of a comment: cyrillic / latin / cjk / other letters, or 'none' for emoji, digits and symbols only."""
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return "none"
    cy = sum("\u0400" <= ch <= "\u04ff" for ch in letters)
    cj = sum("\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff" or "\uac00" <= ch <= "\ud7af" for ch in letters)
    la = sum(ch.isascii() or "\u00c0" <= ch <= "\u024f" for ch in letters)
    top = max((cy, "cyrillic"), (cj, "cjk"), (la, "latin"))
    return top[1] if top[0] / len(letters) >= 0.5 else "other"


def n_words(text):
    return len(re.findall(r"\w+", URL_RX.sub(" ", text)))


# --- Keyword candidates for an episode ------------------------------------------------------------------------------
# Computed at ingest while the text is still held, and stored as a short list of (word, number of comments containing it).
# Only counts are kept, never comment text. Names (Maria, the guest), show words and everyday filler are left out.
STOP = set("""
the and for that this with you your are was were have has had not but all just like what when who how why they them their there then than
from about out can will would could should its it's i'm im dont don't didn cant can't isnt wasnt really very much more most some any
only also even still too one get got going gonna make made makes see saw watch watched want need think thought know feel felt said say says
people person time times way thing things lot every each other another because while after before over into here where which these those
been being does did doing done let lets great good amazing awesome love loved loving best better nice wow thank thanks thankyou thank
yes yeah yep please maybe always never ever again back new old first last next now today week year years day days
podcast podcasts episode episodes video videos show shows interview interviews guest guests host hosts channel conversation conversations
youtube watching listening listen listened pretty tough
she her hers he him his we our us me my mine ours
это как что для все вы мы она он на не но или только очень было был была бы же уже еще ещё будет можно так такой этот мне мой ваш вас спасибо подкаст интервью выпуск гость
""".split())


def keywords(df, top=10, min_count=3):
    """Top words for ONE episode's comments (df columns: comment, about, guest, guest_aliases) as [{"w", "n", "kind"}].
    Work terms that matched (kind 'work') come first in the candidates; plain words are kind 'word'."""
    from collections import Counter
    if df.empty:
        return []
    gname = df["guest"].iloc[0] if "guest" in df and isinstance(df["guest"].iloc[0], str) else ""
    skip = {t for a in str(df["guest_aliases"].iloc[0]).split("|") for t in re.findall(r"\w+", _strip(a))}
    skip |= set(re.findall(r"\w+", _strip(gname)))
    wt, gk = work_terms(), _strip(gname).strip()
    words, work = Counter(), Counter()
    for t, ab in zip(df["comment"], df["about"]):
        vs = variants(t)
        toks = {w for w in re.findall(r"[^\W\d_]{3,}", URL_RX.sub(" ", vs[min(1, len(vs) - 1)]))}
        words.update(w for w in toks if w not in STOP and w not in skip and not MARIA_RX.fullmatch(w))
        named = ab in ("Maria", "Guest", "Both")
        for items in (wt["maria"], wt["guests"].get(gk, [])):
            for rx, risky, kind, term in items:
                if kind != "topic" and _any(rx, vs) and (named or not risky):
                    work[term] += 1
    out = [{"w": w, "n": n, "kind": "work"} for w, n in work.most_common() if n >= min_count]
    seen = set(re.findall(r"\w+", " ".join(x["w"] for x in out)))
    out += [{"w": w, "n": n, "kind": "word"} for w, n in words.most_common(top * 3) if n >= min_count and w not in seen]
    return out[:top]


# --- Requested guests -------------------------------------------------------------------------------------------------
# For comments tagged Request ("Please have Serena Williams on next", "Мария, пригласи Опру"): pull out the name(s) asked for.
# Rule of thumb, not magic: runs of capitalised words (Latin or Cyrillic), minus cue words, Maria's names and the current guest's
# aliases. A comment typed all in lower case falls back to a narrow "invite/have/bring X on" pattern. Russian names are grouped by
# a crude stem (Опра/Опру/Опры). Each name counts once per comment. Stored per episode as {"total", "named", "names":[{"name","n"}]}.
_CAP = "A-Z\u00c0-\u00de\u0410-\u042f\u0401"
CAP_RUN = re.compile(rf"(?<![\w'])[{_CAP}][\w'\u2019\-]*(?: [{_CAP}][\w'\u2019\-]*){{0,3}}")
LOWER_ASK = re.compile(r"\b(?:invite|have|bring|get|interview|host)\s+(?:the\s+)?([a-z][a-z'\-]+(?:\s[a-z][a-z'\-]+){0,2}?)\s+(?:on|to|for|next|in|as)\b")
NAME_STOP = set("""
please can could would will hope hey hi hello love loved i im i'm you your we the this that next maybe pretty tough maria masha sharapova
youtube podcast show episode guest interview invite have bring get when why how what who it its thank thanks yes wow omg her she he his
great amazing and but also need want request suggestion ps sir ms mrs mr dr on to for in as of or with from at by my our us me so if
do does did is are was were be been not no just really very more most some any one two next time ever again too still more
\u043f\u043e\u0436\u0430\u043b\u0443\u0439\u0441\u0442\u0430 \u043f\u0440\u0438\u0433\u043b\u0430\u0441\u0438 \u043f\u0440\u0438\u0433\u043b\u0430\u0441\u0438\u0442\u0435 \u043f\u0440\u0438\u0433\u043b\u0430\u0448\u0430\u0439 \u043f\u0440\u0438\u0433\u043b\u0430\u0448\u0430\u0439\u0442\u0435 \u043f\u043e\u0437\u043e\u0432\u0438 \u043f\u043e\u0437\u043e\u0432\u0438\u0442\u0435 \u0445\u043e\u0447\u0443 \u043f\u0440\u0438\u0432\u0435\u0442 \u0437\u0434\u0440\u0430\u0432\u0441\u0442\u0432\u0443\u0439\u0442\u0435 \u0441\u043f\u0430\u0441\u0438\u0431\u043e \u043c\u0430\u0440\u0438\u044f \u043c\u0430\u0448\u0430 \u0432\u044b \u044f \u043c\u044b \u0430 \u0438 \u043d\u043e \u0434\u043b\u044f \u0431\u044b\u043b\u043e
""".split())
PRONOUNS = set("her him them someone anyone somebody everyone more others".split())


def _name_key(name):
    toks = []
    for t in re.findall(r"[^\W\d_]+", _strip(re.sub(r"['\u2019]s\b", "", name))):
        if re.search("[\u0400-\u04ff]", t) and len(t) >= 4 and t[-1] in "\u0443\u044e\u044b\u0438\u0435\u0430":
            t = t[:-1]                                         # crude stem: Опра / Опру / Опры -> опр
        toks.append(t)
    return " ".join(toks)


def _names_in(text, skip):
    letters = [ch for ch in text if ch.isalpha()]
    upper_share = sum(ch.isupper() for ch in letters) / len(letters) if letters else 0
    found = []
    for run in CAP_RUN.findall(text):
        toks = [re.sub(r"['\u2019]s$", "", t) for t in run.split()]
        while toks and (_strip(toks[0]) in NAME_STOP or _strip(toks[0]) in skip or MARIA_RX.fullmatch(_strip(toks[0]))):
            toks.pop(0)
        while toks and (_strip(toks[-1]) in NAME_STOP or _strip(toks[-1]) in skip or MARIA_RX.fullmatch(_strip(toks[-1]))):
            toks.pop()
        if toks and len(toks) <= 3 and not (upper_share > 0.7 and len(toks) == 1 and len(toks[0]) <= 3):
            found.append(" ".join(toks))
    if not found and text == text.lower():
        m = LOWER_ASK.search(_strip(text))
        if m:
            toks = [t for t in m.group(1).split() if t not in NAME_STOP and t not in PRONOUNS and t not in skip]
            if toks and len(toks) == len(m.group(1).split()):
                found.append(" ".join(w.capitalize() for w in toks))
    return found


NAME_ALIAS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "name_aliases.csv")


def name_aliases():
    """{name key: official name} from name_aliases.csv (columns variant,name), e.g. a Russian spelling -> the English name."""
    out = {}
    if os.path.exists(NAME_ALIAS_FILE):
        for r in _csv_rows(NAME_ALIAS_FILE):
            if (r.get("variant") or "").strip() and (r.get("name") or "").strip():
                out[_name_key(r["variant"])] = r["name"].strip()
    return out


def merge_names(counts, with_map=False):
    """counts: {name: n}. Merges spellings that share a key, and a lone first or last name into the single longer name that
    contains it ("Serena" into "Serena Williams"). Returns {display name: n}, the display being the most common spelling
    (and, with with_map=True, also {original name: display name})."""
    groups, shown, owner = {}, {}, {}
    alias = name_aliases()
    for name0, n in counts.items():
        name = alias.get(_name_key(name0), name0)
        k = _name_key(name)
        groups[k] = groups.get(k, 0) + n
        shown.setdefault(k, {})[name] = n + shown.get(k, {}).get(name, 0)
        owner[name0] = k
        shown[k].setdefault(name, 0)
    multi = [k for k in groups if " " in k]
    for k in [k for k in list(groups) if " " not in k]:
        hit = [m for m in multi if k in m.split()[:1] + m.split()[-1:]]
        if len(hit) == 1:
            groups[hit[0]] += groups.pop(k)
            for nm, c in shown.pop(k).items():
                shown[hit[0]][nm] = shown[hit[0]].get(nm, 0) + c
                owner[nm] = hit[0]
    disp = {k: max(shown[k], key=lambda x: (len(x.split()), shown[k][x])) for k in groups}
    merged = {disp[k]: n for k, n in groups.items()}
    return (merged, {nm: disp[k] for nm, k in owner.items()}) if with_map else merged


def requested_names(df, top=15):
    """df: comment, about, guest, guest_aliases for ONE episode -> {"total", "named", "names": [{"name", "n"}]} (Request comments only)."""
    req = df[df["about"] == "Request"]
    out = {"total": int(len(req)), "named": 0, "names": []}
    if req.empty:
        return out
    skip = {t for a in str(df["guest_aliases"].iloc[0]).split("|") for t in re.findall(r"\w+", _strip(a))}
    if "guest" in df and isinstance(df["guest"].iloc[0], str):
        skip |= set(re.findall(r"\w+", _strip(df["guest"].iloc[0])))
    counts = {}
    for t in req["comment"]:
        names = {_name_key(n): n for n in _names_in(t, skip)}
        out["named"] += bool(names)
        for n in names.values():
            counts[n] = counts.get(n, 0) + 1
    merged = merge_names(counts)
    out["names"] = [{"name": k, "n": v} for k, v in sorted(merged.items(), key=lambda kv: -kv[1])[:top]]
    return out


# --- Address vs discussion (Maria / Guest / Both comments) ---------------------------------------------------------------
# A comment that names Maria is often just talking TO her ("Thanks Maria!", "Great job, Maria"). "address" = short (15 words or
# fewer), second-person or thank/praise wording, and no she/her/he/they; every other Maria/Guest/Both comment is "discussion".
# A rough rule: check it against your own labels (the sampler has a your_mode column). Same rule for the guest's name.
ADDRESS_RX = re.compile(r"\b(you|your|youre|you're|u|ur|thank|thanks|thankyou|congrat\w*|bravo|well done|great job|good job|love you|"
                        r"proud of|miss you|keep it up|keep going)\b|"
                        r"\u0432\u044b|\u0432\u0430\u043c|\u0432\u0430\u0441|\u0432\u0430\u043c\u0438|\u0442\u0435\u0431\u044f|\u0442\u0435\u0431\u0435|\u0441\u043f\u0430\u0441\u0438\u0431\u043e|"
                        r"\u043f\u043e\u0437\u0434\u0440\u0430\u0432\u043b\u044f\w*|\u043c\u043e\u043b\u043e\u0434\u0435\u0446")
THIRD_RX = re.compile(r"\b(she|her|hers|he|him|his|they|them|their)\b")


def _mode_one(text, about):
    if about not in ("Maria", "Guest", "Both"):
        return None
    v = variants(text)[0]
    if n_words(text) <= 15 and ADDRESS_RX.search(v) and not THIRD_RX.search(v):
        return "address"
    return "discussion"


def mode_all(df, about):
    """'address' / 'discussion' for Maria, Guest and Both comments; None for every other tag."""
    return np.array([_mode_one(t, ab) for t, ab in zip(df["comment"], about)], dtype=object)


# --- Alias suggestions --------------------------------------------------------------------------------------------------
# Words in ONE episode's comments that may be another way of naming the guest. Never added automatically: the list goes to
# alias_suggestions.csv for you to copy into guest_aliases. Two kinds:
#   spelling   a word 1-2 edits away from an existing alias (or the guest's name) that 3+ comments use ("ahsley", "jenie")
#   distinctive  a word in 5+ of this episode's comments that is much rarer in the other episodes' comments still held
#              ("giggler"); needs 200+ comments from other episodes to compare against
# Request comments are skipped (they are full of other people's names). Stored per episode as [{"word","n","why"}].
def _edits(a, b, cap=2):
    """Restricted Damerau-Levenshtein distance, giving up above cap."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev2, prev = None, list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = a[i - 1] != b[j - 1]
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
    return prev[-1]


def _doc_freq(texts):
    from collections import Counter
    c = Counter()
    for t in texts:
        c.update(set(re.findall(r"[^\W\d_]{3,}", variants(t)[0])))
    return c


def alias_suggestions(df, background=None, top=8, min_n=3, min_distinct=5):
    """df: comment, about, guest, guest_aliases for ONE episode. background: texts of other episodes (still held), or None."""
    if df.empty:
        return []
    own = df[df["about"] != "Request"] if "about" in df else df
    gname = df["guest"].iloc[0] if "guest" in df and isinstance(df["guest"].iloc[0], str) else ""
    have = {t for a in str(df["guest_aliases"].iloc[0]).split("|") for t in re.findall(r"[^\W\d_]+", _strip(a))}
    base = {t for t in have | set(re.findall(r"[^\W\d_]+", _strip(gname))) if len(t) >= 4}
    fo = _doc_freq(own["comment"])
    n_own = max(len(own), 1)
    fb = _doc_freq(background) if background is not None and len(background) else None
    n_bg = len(background) if fb is not None else 0
    out = {}
    for w, n in fo.items():
        if n < min_n or w in STOP or w in have or w in base or MARIA_RX.fullmatch(w) or re.search("[\u0400-\u04ff]", w):
            continue
        if re.sub(r"(.)\1{2,}", r"\1", w) in have or re.sub(r"(.)\1{2,}", r"\1\1", w) in have:
            continue                                    # a stretched spelling that the matcher already catches
        near = [b for b in base if _edits(w, b, 2 if len(b) > 5 else 1) <= (2 if len(b) > 5 else 1)]
        common = fb is not None and fb.get(w, 0) / max(n_bg, 1) >= 0.5 * n / n_own
        if near and not common:
            out[w] = {"word": w, "n": int(n), "why": f"spelling variant of '{min(near, key=lambda b: _edits(w, b))}'"}
        elif fb is not None and n_bg >= 200 and n >= min_distinct and fb.get(w, 0) / n_bg * 6 <= n / n_own:
            out[w] = {"word": w, "n": int(n), "why": "distinctive: common here, rare in other episodes"}
    return sorted(out.values(), key=lambda x: (x["why"].startswith("distinctive"), -x["n"]))[:top]


def _meta(df):
    cols = ["video_id", "guest_aliases"] + (["guest"] if "guest" in df else [])
    d = df.drop_duplicates("video_id")[cols]
    rx = {r.video_id: alias_regex(r.guest_aliases) for r in d.itertuples()}
    gk = {r.video_id: _strip(r.guest).strip() if "guest" in df and isinstance(r.guest, str) else "" for r in d.itertuples()}
    return rx, gk


def classify(df):
    """df needs columns: video_id, guest_aliases (pipe-separated), comment (and guest, for Work terms). Returns an array of categories."""
    rx, gk = _meta(df)
    return np.array([_one(t, rx[v], gk[v]) for v, t in zip(df["video_id"], df["comment"])])


def flags_all(df, about):
    """(work, topic) arrays: work = Maria / Guest / Both / None, topic = 'doping' / None. Neither changes the tag except Work-only."""
    rx, gk = _meta(df)
    w, tp = [], []
    for v, t, ab in zip(df["video_id"], df["comment"], about):
        a, _, b = _work_flags(variants(t), ab, gk[v])
        w.append(a); tp.append(b)
    return np.array(w, dtype=object), np.array(tp, dtype=object)


# --- Focus of a "Both" comment -------------------------------------------------------------------------------
# A Both comment names Maria and the guest, but is often really about one of them ("Maria, great questions... with Zoe").
# Rough rule fitted to 34 hand-labelled Both comments (74% agreement; optimistic because it was tuned on them):
#   Guest : the guest is named more often than Maria and "she/her" appears 3+ times
#   Maria : 100+ words and either 3+ host words (interview, questions, host...) or the guest is not named more than Maria
#   Joint : everything else (mostly short "thanks Maria, great chat with X" comments)
HOST_RX = re.compile(r"\b(host|hosting|interviewer|interview|questions?|guided|asked|asking|listen\w*|podcast|episode|subscri\w+|series)\b")
SHE_RX = re.compile(r"\b(she|her|hers)\b")


def _focus_one(text, grx, about):
    if about != "Both":
        return None
    v = variants(text)[0]
    m = len(MARIA_RX.findall(v))
    g = len(grx.findall(v)) if grx else 0
    she, host, words = len(SHE_RX.findall(v)), len(HOST_RX.findall(v)), len(v.split())
    if g >= m + 1 and she >= 3:
        return "Guest"
    if words >= 100 and (host >= 3 or g <= m):
        return "Maria"
    return "Joint"


def focus_all(df, about):
    """Focus (Maria / Guest / Joint) for Both comments, None for everything else."""
    rx = {v: alias_regex(a) for v, a in df.drop_duplicates("video_id")[["video_id", "guest_aliases"]].values}
    return np.array([_focus_one(t, rx[v], ab) for v, t, ab in zip(df["video_id"], df["comment"], about)], dtype=object)

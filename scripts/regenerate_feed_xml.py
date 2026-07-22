#!/usr/bin/env python3
"""
Regenerate static/podcast/feed.xml for the Crown of Aragon podcast.

Run:  python3 scripts/regenerate_feed_xml.py

Output: static/podcast/feed.xml
"""

import os
import sys
import xml.etree.ElementTree as ET

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_PATH = os.path.join(PROJECT_ROOT, "static", "podcast", "feed.xml")

# ── helpers ──────────────────────────────────────────────────────────────────

def cdata_safe(text):
    """Wrap text that contains XML-special characters in CDATA."""
    if any(ch in text for ch in ("&", "<", ">", "'", '"')):
        return f"<![CDATA[{text}]]>"
    return text


def indent_xml(elem, level=0):
    """Pretty-print an ElementTree element. Does NOT indent CDATA children."""
    indent = "    "
    in_cdata_block = False
    if len(elem):
        if not (elem.text and elem.text.strip()) and not in_cdata_block:
            elem.text = "\n" + indent * (level + 1)
        for child in elem:
            indent_xml(child, level + 1)
        if not (elem.tail and elem.tail.strip()) and not in_cdata_block:
            elem.tail = "\n" + indent * level
    else:
        if level and (not elem.tail or not elem.tail.strip()):
            elem.tail = "\n" + indent * level


# ── channel metadata ─────────────────────────────────────────────────────────

CHANNEL_TITLE = "The Crown of Aragon: A Singular Mediterranean Empire"
CHANNEL_LINK = "https://poliscopic.com/podcast"
CHANNEL_DESC = (
    "A podcast series working through the 2017 academic volume "
    "'The Crown of Aragon: A Singular Mediterranean Empire', chapter by chapter."
)
CHANNEL_LANGUAGE = "en-us"
CHANNEL_AUTHOR = "Aristotle"
CHANNEL_CATEGORY = "History"
CHANNEL_EXPLICIT = "no"
CHANNEL_IMAGE = "https://poliscopic.com/podcast/podcast-cover.jpg"

# ── episode data ─────────────────────────────────────────────────────────────
# (title, guid, duration, length, enclosure_url, pub_date)
#
# Each episode also gets a human-written description (~100 words) that states
# the thesis and key questions without AI tells.

EPISODES = [
    {
        "title": "A Singular Mediterranean Empire \u2014 The Analytical Framework",
        "guid": "crown-aragon-episode-01-sabate-ch1-expanded",
        "duration": "13:01",
        "length": "7299045",
        "enclosure": "https://archive.org/download/crown-of-aragon-podcast-v4/episode-01-sabate-ch1-expanded.mp3",
        "pubDate": "Tue, 07 Jul 2026 21:01:53 +0000",
        "description": (
            "Flocel Sabat\u00e9\u2019s opening chapter lays the analytical foundation for understanding "
            "the Crown of Aragon not as a kingdom in the conventional sense, but as a composite "
            "monarchy\u2014a dynastic confederation of distinct territories bound by a shared sovereign "
            "yet retaining their own laws, currencies, and representative bodies. The central thesis "
            "is that this institutional fragmentation was not a weakness but the very mechanism that "
            "enabled Mediterranean expansion. What does it mean to call a medieval polity \u201csingular\u201d? "
            "How did the Crown differ from its contemporaries\u2014Castile, France, England\u2014in ways "
            "that defined its trajectory from the 12th through the 15th centuries?"
        ),
    },
    {
        "title": "The Miscalculation That Changed Iberia",
        "guid": "crown-aragon-episode-02-brufal-ch2-revised",
        "duration": "16:38",
        "length": "9279069",
        "enclosure": "https://archive.org/download/crown-of-aragon-podcast-v4/episode-02-brufal-ch2-revised.mp3",
        "pubDate": "Tue, 07 Jul 2026 21:03:29 +0000",
        "description": (
            "Jes\u00fas Brufal examines the Muslim-ruled northeast of the Iberian Peninsula before "
            "the Christian conquests reshaped it entirely. The taifa kingdoms of the Ebro valley "
            "were more than military obstacles\u2014they were sophisticated urban societies whose "
            "irrigation infrastructure, fiscal systems, and trade networks became the substrate "
            "on which later Aragonese and Catalan power was built. The miscalculation in question "
            "is the fracturing of the Caliphate into competing taifa states after 1031, which turned "
            "a formidable unified power into a set of tribute-paying clients ripe for absorption. "
            "How much of the Crown\u2019s eventual wealth was inherited from the very societies it displaced?"
        ),
    },
    {
        "title": "The Accident That Created a Crown",
        "guid": "crown-aragon-episode-03-kosto-revised",
        "duration": "14:39",
        "length": "8209389",
        "enclosure": "https://archive.org/download/crown-of-aragon-podcast-v4/episode-03-kosto-revised.mp3",
        "pubDate": "Tue, 07 Jul 2026 21:04:53 +0000",
        "description": (
            "Adam Kosto argues that the union of Aragon and the Catalan counties in 1137 was anything "
            "but inevitable. The betrothal of the infant Petronilla of Aragon to the adult Count "
            "Ramon Berenguer IV of Barcelona was a rushed, politically desperate arrangement, not "
            "a grand nation-building project. Aragon had just lost its king at the Battle of Fraga "
            "and faced absorption by Castile or Navarre; the Catalans saw opportunity but carried "
            "the baggage of their own Occitan entanglements. The chapter asks whether the Crown of "
            "Aragon was born from ambition or from a sequence of accidents\u2014and whether most "
            "medieval composite monarchies started the same way."
        ),
    },
    {
        "title": "The Occitan Dream, Cut Short at Muret",
        "guid": "crown-aragon-episode-04-benito-revised",
        "duration": "11:48",
        "length": "6599181",
        "enclosure": "https://archive.org/download/crown-of-aragon-podcast-v4/episode-04-benito-revised.mp3",
        "pubDate": "Tue, 07 Jul 2026 21:06:02 +0000",
        "description": (
            "Pere Benito traces the Crown\u2019s intense but ultimately failed campaign to extend its "
            "dominion across the Pyrenees into Occitania. For nearly a century after the dynastic "
            "union, the counts of Barcelona treated the lands north of the mountains\u2014Provence, "
            "Millau, G\u00e9vaudan\u2014as integral to their patrimony. The Albigensian Crusade changed "
            "everything. King Peter II died at Muret in 1213 fighting Simon de Montfort\u2019s "
            "crusader army, and with him died the Occitan project. The episode focuses on a single "
            "question: what did Aragon lose when it lost Occitania\u2014and what did it gain by being "
            "forced to turn its attention southward and seaward instead?"
        ),
    },
    {
        "title": "The Simon de Montfort Question",
        "guid": "crown-aragon-episode-04-montfort-question",
        "duration": "11:24",
        "length": "6428661",
        "enclosure": "https://archive.org/download/crown-of-aragon-podcast-v4/episode-04-montfort-question.mp3",
        "pubDate": "Tue, 07 Jul 2026 21:07:10 +0000",
        "description": (
            "Simon de Montfort is the connective tissue between two of the 13th century\u2019s most "
            "significant constitutional experiments: the Crown of Aragon and the Kingdom of England. "
            "The father led the Albigensian Crusade and killed King Peter II at Muret in 1213, "
            "slamming the door on Aragonese Occitania. The son\u2014also Simon de Montfort\u2014led the "
            "baronial revolt against Henry III and summoned the 1265 Parliament that included "
            "burgesses for the first time. This episode examines whether the parallel is coincidence "
            "or something deeper: did the political DNA of the Occitan nobility carry a tradition "
            "of pactism and contractual governance into both Mediterranean and Atlantic contexts?"
        ),
    },
    {
        "title": "Clerics and Troubadours",
        "guid": "crown-aragon-episode-05-grifoll",
        "duration": "10:57",
        "length": "6158421",
        "enclosure": "https://archive.org/download/crown-of-aragon-podcast-v4/episode-05-grifoll.mp3",
        "pubDate": "Tue, 07 Jul 2026 21:08:14 +0000",
        "description": (
            "Isabel Grifoll examines the cultural landscape of the Crown from the 9th through "
            "12th centuries, a world in which monasteries functioned as the primary engines of "
            "textual production and troubadours as the carriers of vernacular literary culture "
            "across linguistic boundaries. The Ripoll scriptorium, the poetic courts of the "
            "Catalan-Aragonese nobility, the influx of Occitan lyric forms\u2014these were not "
            "ornaments but instruments of political identity. The chapter asks how a frontier "
            "society at the edge of Christendom produced a courtly culture sophisticated enough "
            "to absorb and transmit the troubadour tradition, and what the clerical-monastic "
            "foundations beneath it reveal about who actually held cultural authority."
        ),
    },
    {
        "title": "Romanesque in the Mountains and on the Border",
        "guid": "crown-aragon-episode-06-barral-ch6",
        "duration": "14:00",
        "length": "7838565",
        "enclosure": "https://archive.org/download/crown-of-aragon-podcast-v4/episode-06-barral-ch6.mp3",
        "pubDate": "Tue, 07 Jul 2026 21:09:36 +0000",
        "description": (
            "Xavier Barral i Altet argues that the distinctive Romanesque architecture of the "
            "Pyrenean valleys\u2014Sant Climent de Ta\u00fcll, Santa Maria de Ripoll, the cathedral "
            "of La Seu d\u2019Urgell\u2014was not a provincial echo of French models but a frontier "
            "expression shaped by its position between Christendom and al-Andalus. The churches "
            "of the Bo\u00ed Valley are small, remote, and archaeologically extraordinary, built "
            "with wealth extracted from the reconquered lowlands. Barral asks what the builders "
            "were signalling: piety, certainly, but also territorial control, dynastic legitimacy, "
            "and a visual claim that this land was now permanently Christian even while the "
            "border remained contested."
        ),
    },
    {
        "title": "Territory, Power and Institutions",
        "guid": "crown-aragon-episode-07-sabate-ch7",
        "duration": "19:04",
        "length": "10713645",
        "enclosure": "https://archive.org/download/crown-of-aragon-podcast-v4/episode-07-sabate-ch7.mp3",
        "pubDate": "Tue, 07 Jul 2026 21:11:29 +0000",
        "description": (
            "Flocel Sabat\u00e9 returns to examine how the Crown\u2019s composite structure actually worked "
            "in practice: not through bureaucracy or standing armies but through a negotiated "
            "distribution of power formalized in the Corts of each constituent territory. The "
            "Generalitat of Catalonia, the Diputaci\u00f3n of Aragon, the representative bodies of "
            "Valencia and Mallorca\u2014these were not symbolic assemblies but institutions with real "
            "fiscal authority and the capacity to constrain the monarch. How did pactism\u2014the "
            "principle that royal power was conditional on consent\u2014survive when absolutist "
            "models were taking hold elsewhere in Europe? And did it actually work, or was it a "
            "fiction maintained by elites for their own benefit?"
        ),
    },
    {
        "title": "Urban Manufacturing and Long-Distance Trade",
        "guid": "crown-aragon-episode-08-riera-ch8",
        "duration": "18:57",
        "length": "10647429",
        "enclosure": "https://archive.org/download/crown-of-aragon-podcast-v4/episode-08-riera-ch8.mp3",
        "pubDate": "Tue, 07 Jul 2026 21:13:25 +0000",
        "description": (
            "Antoni Riera traces the emergence of Barcelona\u2019s commercial economy from its "
            "origins in the 12th and 13th centuries, when the city transformed from a modest "
            "port into the Mediterranean\u2019s most dynamic textile and trading centre. The key "
            "shift was the development of urban manufacturing\u2014woollens, then silks\u2014that produced "
            "goods worth carrying across the sea rather than simply transhipping other people\u2019s "
            "merchandise. Riera asks how a relatively small city on the western edge of the "
            "Mediterranean managed to compete with Genoa and Venice. The answer involves "
            "institutional innovation\u2014the Consulate of the Sea, commercial courts, the "
            "llibre del Consolat\u2014as much as geographic position."
        ),
    },
    {
        "title": "Crises and Changes in the Late Middle Ages",
        "guid": "crown-aragon-episode-09-riera-ch9",
        "duration": "18:50",
        "length": "10580445",
        "enclosure": "https://archive.org/download/crown-of-aragon-podcast-v4/episode-09-riera-ch9.mp3",
        "pubDate": "Tue, 07 Jul 2026 21:15:22 +0000",
        "description": (
            "Riera continues with the 14th century\u2014a period of demographic collapse, banking "
            "failures, and structural economic contraction across Europe. The Crown was not immune. "
            "The Black Death hit Barcelona in 1348, killing perhaps a third of the population, "
            "and the collapse of the Bardi and Peruzzi banks in Florence sent shockwaves through "
            "Catalan commercial networks. Yet Riera\u2019s thesis is that crisis produced adaptation, "
            "not collapse: the shift from high-volume woollens to luxury textiles, the "
            "consolidation of the municipal debt market, and the emergence of a new fiscal "
            "infrastructure that would prove surprisingly resilient. What broke, what bent, "
            "and what actually grew stronger under pressure?"
        ),
    },
    {
        "title": "Commercial Influence in the Eastern Mediterranean",
        "guid": "crown-aragon-episode-10-coulon-ch10",
        "duration": "17:45",
        "length": "9962253",
        "enclosure": "https://archive.org/download/crown-of-aragon-podcast-v4/episode-10-coulon-ch10.mp3",
        "pubDate": "Tue, 07 Jul 2026 21:17:13 +0000",
        "description": (
            "Damien Coulon maps the Crown\u2019s commercial footprint across the eastern Mediterranean "
            "in the 14th and 15th centuries, from the consulates established in Alexandria and "
            "Beirut to the merchant colonies of Constantinople and Famagusta. The thesis is "
            "that Catalan commercial penetration was deeper and more durable than its relatively "
            "modest military presence would suggest. Consular records, notarial registers, and "
            "port books reveal a dense network of agents, factors, and partnerships that moved "
            "spices, cotton, and alum through the Levant. The episode asks whether the Crown "
            "built an empire of trade to compensate for what it could not conquer by force\u2014and "
            "how long that model remained viable as Ottoman power consolidated."
        ),
    },
    {
        "title": "A Critical Reading of the Commercial Empire",
        "guid": "crown-aragon-episode-10-critical-reading",
        "duration": "17:55",
        "length": "10033437",
        "enclosure": "https://archive.org/download/crown-of-aragon-podcast-v4/episode-10-critical-reading.mp3",
        "pubDate": "Tue, 07 Jul 2026 21:19:07 +0000",
        "description": (
            "This episode steps back from Coulon\u2019s thesis to ask harder questions about the "
            "evidence. How much of the \u201ccommercial empire\u201d narrative rests on a handful of "
            "well-documented consulates while ignoring the many ports where Catalan presence "
            "was thin or absent? The consular registers from Alexandria are detailed, but they "
            "cover specific decades; extrapolating from them to the entire eastern Mediterranean "
            "may overstate the Crown\u2019s reach. Did the Catalans genuinely compete with Venice "
            "and Genoa, or did they operate in the niches those powers left open? What does "
            "\u201cinfluence\u201d mean when you have a consul in a city but no meaningful market share "
            "in the goods moving through it?"
        ),
    },
    {
        "title": "Labourers and Rulers in an Expanding Society",
        "guid": "crown-aragon-episode-11-bonet-ch11",
        "duration": "28:05",
        "length": "15780477",
        "enclosure": "https://archive.org/download/crown-of-aragon-podcast-v4/episode-11-bonet-ch11.mp3",
        "pubDate": "Tue, 07 Jul 2026 21:22:05 +0000",
        "description": (
            "Maria Bonet examines the social structure beneath the institutional and commercial "
            "narratives: the peasants who worked the land, the artisans who populated the cities, "
            "the nobles who extracted surplus, and the clergy who mediated between them. The "
            "remen\u00e7a question\u2014Catalan serfs bound to the land and subject to the \u201cbad customs\u201d "
            "or mals usos\u2014becomes a lens for understanding how territorial expansion and "
            "commercial wealth affected the people at the bottom. As Barcelona grew rich on "
            "Mediterranean trade, the countryside seethed. Bonet asks whether the Crown\u2019s "
            "Mediterranean success was built on a social order at home that was becoming "
            "unsustainable by the 15th century."
        ),
    },
    {
        "title": "Islands, White Elephants, and the Cost of Empire",
        "guid": "crown-aragon-episode-12-cioppi-nocco-ch12",
        "duration": "17:49",
        "length": "9983229",
        "enclosure": "https://archive.org/download/crown-of-aragon-podcast-v4/episode-12-cioppi-nocco-ch12.mp3",
        "pubDate": "Thu, 09 Jul 2026 01:21:48 +0000",
        "description": (
            "Alessandra Cioppi and Sebastiana Nocco examine the Crown\u2019s island possessions\u2014"
            "Mallorca, Sardinia, Sicily\u2014as strategic assets and financial liabilities. Controlling "
            "the western Mediterranean islands meant controlling sea lanes, grain supplies, and "
            "salt production, but each island came with a restive population, a garrison to "
            "finance, and local elites who had to be bought off or broken. Sardinia in particular "
            "was a running sore: decades of rebellion, punitive campaigns, and ruinous expenditure "
            "for uncertain returns. The chapter asks whether the islands were the foundation of "
            "maritime empire or magnificent distractions that drained resources better spent "
            "on the Crown\u2019s commercial and peninsular interests."
        ),
    },
    {
        "title": "Ramon Llull: The Prophet Who Failed",
        "guid": "crown-aragon-episode-13-ramon-llull-kokoro",
        "duration": "13:53",
        "length": "13334061",
        "enclosure": "https://archive.org/download/crown-of-aragon-podcast-v4/episode-13-ramon-llull-kokoro.mp3",
        "pubDate": "Sat, 11 Jul 2026 12:00:00 +0000",
        "description": (
            "Ramon Llull (c. 1232\u20131316) was a Mallorcan courtier-turned-mystic who spent the "
            "second half of his life trying to convert the Muslim and Jewish worlds to Christianity "
            "through rational demonstration. He wrote nearly 300 works across three languages, "
            "invented a combinatorial logical system he believed could prove the articles of "
            "faith, and repeatedly travelled to North Africa where he was stoned and expelled "
            "rather than embraced. His project was magnificent in ambition and total in practical "
            "failure. This episode examines Llull as a product of the Crown\u2019s cultural conditions: "
            "a trilingual borderland society where the possibility of rational conversion seemed "
            "plausible\u2014and why it never was."
        ),
    },
    {
        "title": "The Man Who Saw Too Much: Critiquing the Vita Coetanea",
        "guid": "crown-aragon-episode-13-vita-critique",
        "duration": "13:02",
        "length": "7176885",
        "enclosure": "https://archive.org/download/crown-of-aragon-podcast-v4/episode-13-vita-critique.mp3",
        "pubDate": "Sat, 11 Jul 2026 17:50:05 +0000",
        "description": (
            "The Vita Coetanea is Llull\u2019s autobiographical account of his conversion, dictated "
            "to Carthusian monks in Paris in 1311. For centuries it was read as straight "
            "hagiography\u2014the sinner turned saint by a series of visions. Josep Ruiz and Albert "
            "Soler dismantle that reading. They show that the Vita is a carefully constructed "
            "self-presentation, written by a man in his late 70s who had failed to convince "
            "popes, kings, and councils of his mission and was now trying to cement his legacy "
            "through the one institution he still trusted: the Carthusian scriptorium. What "
            "did Llull omit, invent, or rearrange to make his life look like a coherent divine "
            "plan rather than a series of setbacks?"
        ),
    },
]


# ── build XML ────────────────────────────────────────────────────────────────

def build_feed():
    rss = ET.Element("rss", {
        "version": "2.0",
        "xmlns:itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
        "xmlns:content": "http://purl.org/rss/1.0/modules/content/",
    })
    channel = ET.SubElement(rss, "channel")

    ET.SubElement(channel, "title").text = CHANNEL_TITLE
    ET.SubElement(channel, "link").text = CHANNEL_LINK
    ET.SubElement(channel, "description").text = CHANNEL_DESC
    ET.SubElement(channel, "language").text = CHANNEL_LANGUAGE

    author_elem = ET.SubElement(channel, "{http://www.itunes.com/dtds/podcast-1.0.dtd}author")
    author_elem.text = CHANNEL_AUTHOR

    cat_elem = ET.SubElement(channel, "{http://www.itunes.com/dtds/podcast-1.0.dtd}category")
    cat_elem.set("text", CHANNEL_CATEGORY)

    expl_elem = ET.SubElement(channel, "{http://www.itunes.com/dtds/podcast-1.0.dtd}explicit")
    expl_elem.text = CHANNEL_EXPLICIT

    img_elem = ET.SubElement(channel, "{http://www.itunes.com/dtds/podcast-1.0.dtd}image")
    img_elem.set("href", CHANNEL_IMAGE)

    for ep in EPISODES:
        item = ET.SubElement(channel, "item")

        ET.SubElement(item, "title").text = ep["title"]

        it_title = ET.SubElement(item, "{http://www.itunes.com/dtds/podcast-1.0.dtd}title")
        it_title.text = ep["title"]

        ET.SubElement(item, "description").text = cdata_safe(ep["description"])

        it_summary = ET.SubElement(item, "{http://www.itunes.com/dtds/podcast-1.0.dtd}summary")
        it_summary.text = cdata_safe(ep["description"])

        it_author = ET.SubElement(item, "{http://www.itunes.com/dtds/podcast-1.0.dtd}author")
        it_author.text = CHANNEL_AUTHOR

        it_expl = ET.SubElement(item, "{http://www.itunes.com/dtds/podcast-1.0.dtd}explicit")
        it_expl.text = CHANNEL_EXPLICIT

        it_dur = ET.SubElement(item, "{http://www.itunes.com/dtds/podcast-1.0.dtd}duration")
        it_dur.text = ep["duration"]

        enc = ET.SubElement(item, "enclosure")
        enc.set("url", ep["enclosure"])
        enc.set("length", ep["length"])
        enc.set("type", "audio/mpeg")

        guid = ET.SubElement(item, "guid")
        guid.set("isPermaLink", "false")
        guid.text = ep["guid"]

        ET.SubElement(item, "pubDate").text = ep["pubDate"]

    return rss


# ── serialise ────────────────────────────────────────────────────────────────

def serialise_pretty(rss):
    """Hand-roll pretty XML because ElementTree mangling of iTunes namespaces is real."""
    ET.indent(rss, space="    ")

    xml_bytes = ET.tostring(rss, encoding="unicode")

    # ── Post-process for consistent formatting ─────────────────────────────
    lines = xml_bytes.splitlines()

    # Insert XML declaration
    lines.insert(0, '<?xml version="1.0" encoding="utf-8"?>')

    # Replace any double-escaped CDATA that ElementTree might have produced
    # ElementTree serialises & as &amp; inside text nodes, but not inside
    # actual CDATA sections that were set as text.  Our cdata_safe returns the
    # literal CDATA string, so ElementTree will escape the <! markers.  We
    # need to post-process those back.
    raw = "\n".join(lines)
    raw = raw.replace("&lt;![CDATA[", "<![CDATA[")
    raw = raw.replace("]]&gt;", "]]>")

    return raw


def main():
    rss = build_feed()
    xml_str = serialise_pretty(rss)

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(xml_str)

    print(f"Wrote {OUTPUT_PATH}  ({len(xml_str):,} bytes)")


if __name__ == "__main__":
    main()

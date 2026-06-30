"""Prompts for the deep researcher — ported from dzhng/deep-research, adapted to the Bible graph.

Kept deliberately short (dzhng's prompts are ~5 lines each and work well). The system prompt frames
an expert biblical-studies analyst; the task prompts drive structured output (see core.research.nodes
for the schemas). Every learning is asked to carry scripture references and named entities so the
final report can cite chapter:verse.
"""
from __future__ import annotations

import datetime

_SYSTEM = """You are an expert biblical-studies researcher working over a knowledge graph built from \
the text of the Bible (entities = people, places, things; relationships between them; thematic \
community summaries; and the underlying passages). Today is {date}.
- The user is an experienced analyst — be detailed and precise, do not oversimplify.
- Ground every claim in the corpus: cite books, chapters and verses (e.g. Genesis 1:1) and name the \
entities involved.
- Be accurate and thorough; mistakes erode trust. Distinguish what the text states from \
interpretation, and explicitly flag any speculation."""


def system_prompt() -> str:
    """The shared system prompt, stamped with today's date."""
    return _SYSTEM.format(date=datetime.date.today().isoformat())


GENERATE_QUERIES = """Given the research brief below, generate up to {n} unique search queries that, \
answered together, would thoroughly research it. Each query must be self-contained and answerable \
from the Bible knowledge graph. Make them distinct — do not overlap. For each, state the goal (what \
it should establish and what to investigate next once its results are in) AND a scholar_query: the \
same information need expressed as academic search keywords for a scholarly literature database \
(English, domain terminology — named figures, places, periods, methods, theological concepts; no \
question words), used to pull complementary peer-reviewed research.

<brief>{seed}</brief>

Learnings so far (use them to go deeper and fill gaps; do not repeat what is already known):
{learnings}"""


READ_ARTICLE = """You are given the {provenance} of ONE scholarly work returned for the query \
<query>{query}</query>, in service of this research brief:

<brief>{brief}</brief>

<work>{meta}</work>
<contents>
{contents}
</contents>

First decide whether this work is genuinely relevant to the brief (not merely keyword-adjacent). If \
it is NOT relevant, set relevant=false and leave the other fields empty. If it IS relevant:
- summary: a cohesive, self-contained synthesis of the work's central argument and findings — what it \
claims and how it supports it. Attribute to the author(s); distinguish the work's interpretation from \
the biblical text itself; invent nothing absent from the contents. If only an abstract was available, \
summarize what it asserts and keep it brief.
- references: up to {n_refs} key sources this work itself relies on (author/year/title as they appear \
in the contents), each with a short note on what it supports — these let a reader validate and trace \
the work's claims. List only references actually present in the contents; if none are discernible, \
return an empty list.
- research_queries: up to {n_queries} specific new research questions this work opens up, worth \
investigating next over the Bible knowledge graph or in further literature."""


PROCESS_RESULT = """Below is the answer a GraphRAG search returned for the query <query>{query}</query>. \
Extract up to {n_learnings} unique, information-dense learnings — each self-contained, including \
scripture references (book chapter:verse), named entities (people, places, things), and any exact \
names, numbers or dates. Then propose up to {n_follow} specific follow-up questions worth \
researching next to deepen or fill gaps in the topic.

<contents>
{contents}
</contents>"""


FINAL_REPORT = """Write a detailed Markdown research report answering the brief, using ONLY the \
learnings below. Be comprehensive (aim for several sections); weave the learnings into prose rather \
than listing them; cite scripture references inline (book chapter:verse). Use # for the title, ## \
for sections, ### for subsections. Do not invent facts beyond the learnings.

<brief>{prompt}</brief>

<learnings>
{learnings}
</learnings>"""


FINAL_ANSWER = """Answer the question below concisely using the learnings. Output only the answer, in \
the format the question implies — no preamble, no commentary. Usually this should be a few words to \
a single sentence.

<question>{prompt}</question>

<learnings>
{learnings}
</learnings>"""

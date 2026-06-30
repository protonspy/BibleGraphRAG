"""Deep researcher over the GraphRAG index.

A recursive research loop (modelled on dzhng/deep-research) wired as a LangGraph StateGraph: plan
sub-queries, classify each into a GraphRAG search method (global/local/drift/basic), run it, distil
the answer into dense learnings (the raw answer is never carried forward), then optionally recurse
into follow-up questions before synthesizing a final report. See docs/langchain-langgraph.md.
"""

PAPER_EXTRACTION_SYSTEM = """You extract scientific paper knowledge into JSON for a local research wiki.
Do not invent information. Use null, empty strings, or [] when the paper does not provide an answer. Extract author-provided keywords only (do not invent keywords). Experiment details, method principles and dataset statistics must be explicitly supported by source text. A standalone method must be a recognizable named approach, not a generic component or bare keyword. Keep topic labels broad and coherent.
Distinguish author claims from your own summary. Keep entity names concise and canonical.
Return only valid JSON with this shape:
{
  "title": "string",
  "authors": ["string"],
  "year": 2024,
  "abstract": "original text or null",
  "abstract_zh": "faithful Simplified Chinese translation or empty string",
  "keywords": ["author-provided keyword"],
  "experiment_info": "experiment settings explicitly stated in the paper",
  "research_problem": "string",
  "methodology": "string",
  "methods": [{"name": "original method name", "name_zh": "Chinese label", "description": "string", "role": "string", "principle": "string"}],
  "datasets": [{"name": "original dataset name", "name_zh": "Chinese label when appropriate", "description": "string", "role": "string", "statistics": "string"}],
  "metrics": ["string"],
  "contributions": ["string"],
  "findings": ["string"],
  "limitations": ["string"],
  "topics": ["original canonical research topic"],
  "topic_labels_zh": {"original canonical research topic": "concise Chinese label"}
}"""


DEEP_EXTRACTION_SYSTEM = """You analyze a scientific paper to enrich an existing offline metadata record.
Return ONLY a JSON object. Use the source text, not guesses or prior training knowledge.
Do not reconstruct the abstract or invent author keywords. Leave unsupported claims blank.
The desired schema is:
{
  "abstract_zh": "faithful Simplified Chinese rendering of the source abstract, or empty string",
  "research_problem": "string",
  "methodology": "string",
  "methods": [{"name": "original canonical method name", "name_zh": "short Chinese equivalent", "description": "definition from the paper", "role": "application", "principle": "explicit mechanism"}],
  "datasets": [{"name": "original dataset name", "name_zh": "short Chinese equivalent if helpful", "description": "string", "role": "verified experimental usage or mention", "statistics": "explicit source numbers only"}],
  "experiment_info": "datasets, setup, baselines and metrics as explicitly stated",
  "metrics": ["string"],
  "contributions": ["string"],
  "findings": ["string"],
  "limitations": ["string"],
  "topics": ["broad topic name in its original established form"],
  "topic_labels_zh": {"topic name above": "Chinese label"}
}
Avoid generic keywords as methods. Do not infer experiment usage from a dataset name appearing only in related work.
Differentiate the authors' stated claims from independently established facts.
Every statement must be supported by the supplied paper text. Missing details become empty strings/lists.
Never output an unsupported numerical claim. No markdown fences or commentary."""


PAPER_ANALYSIS_SYSTEM = """You are preparing a scientific document for a traceable research wiki.
Produce concise final analysis notes only; do not reveal chain-of-thought.
Identify the document title, key entities, methods, datasets, claims, limitations,
and possible ambiguities. Mark missing or uncertain facts explicitly. Do not invent."""


def paper_analysis_prompt(text: str, purpose: str = "") -> str:
    limit = 50000
    excerpt = text if len(text) <= limit else text[:38000] + "\n...[middle omitted]...\n" + text[-12000:]
    purpose_block = ("WIKI PURPOSE:\n" + purpose.strip() + "\n\n") if purpose.strip() else ""
    return purpose_block + "Analyze this paper before structured extraction.\n\nPAPER TEXT:\n" + excerpt


def paper_extraction_prompt(text: str, analysis: str = "", purpose: str = "") -> str:
    # Keep prompts bounded. The beginning contains title/abstract/method context in most papers;
    # the tail often contains findings and limitations.
    limit = 50000
    excerpt = text if len(text) <= limit else text[:38000] + "\n...[middle omitted]...\n" + text[-12000:]
    blocks = ["Extract the paper into the requested JSON schema."]
    if purpose.strip():
        blocks.extend(["WIKI PURPOSE:", purpose.strip()])
    if analysis.strip():
        blocks.extend([
            "STAGE 1 ANALYSIS (context only; verify every field against the paper text):",
            analysis.strip(),
        ])
    blocks.extend(["PAPER TEXT:", excerpt])
    return "\n\n".join(blocks)


QA_SYSTEM = """You answer questions using only the supplied ResearchWiki pages.
Be explicit when evidence is insufficient. Preserve disagreements between papers.
Use inline numbered citations in the form [1], [2], matching the supplied SOURCE numbers,
and finish with a Sources section. Never cite a source number that was not supplied.
Do not fabricate page numbers or claims."""



def extraction_language_rules(language: str = "zh-CN") -> str:
    """Default Chinese prose without altering bibliographic/source identity."""
    if not str(language).lower().startswith("zh"):
        return "\nWrite explanatory prose in English. Preserve source wording and original entity names."
    return """
OUTPUT LANGUAGE POLICY — SIMPLIFIED CHINESE FIRST:
- Write generated interpretation in clear Simplified Chinese: research_problem,
  methodology, experiment_info, contributions, findings, limitations, and the
  description/role/principle/statistics of methods and datasets.
- Preserve the exact original paper title, author names, year, abstract,
  author-supplied keywords, dataset/method names, model acronyms, DOI and
  evidence quotations. Do not silently replace original names with translations.
- If the source contains a readable abstract, provide its faithful Chinese
  rendering in abstract_zh. Never overwrite the original abstract. If no
  abstract exists in the source, return an empty abstract_zh.
- Optional name_zh and topic_labels_zh are human-readable Chinese display labels,
  not new entities or facts. Keep canonical names in name/topics unchanged.
- Do not infer experimental use from a mention alone, invent translations of
  missing text, fabricate results or alter numeric values.
"""


def answer_language_rules(language: str = "zh-CN") -> str:
    if not str(language).lower().startswith("zh"):
        return "\nAnswer in English unless the user expressly requests another language."
    return """
Answer in natural Simplified Chinese by default, even if the retrieved papers
are in English. Keep paper titles, named methods, datasets, abbreviations,
citations, DOIs, numbers and verbatim evidence quotations in their source
language. Translate explanations rather than rewriting original evidence.
If the user explicitly requests another language, follow that request.
Do not add unsupported statements or pretend a translation was verified.
"""

"""Evidence handles shared by office and NPC generation."""

import re

from .providers import ProviderError


def normalize_citations(answer, evidence):
    identifiers = [item["id"] for item in evidence]

    def expand(match):
        prefix = match.group(1)
        matches = [identifier for identifier in identifiers if identifier.startswith(prefix)]
        if len(matches) == 1:
            return "[" + matches[0] + "]"
        if evidence or "-" in prefix:
            raise ProviderError("回答包含无效或歧义来源标识，未提交该回答。")
        return match.group(0)

    def handle(match):
        index = int(match.group(1)) - 1
        if not 0 <= index < len(identifiers):
            raise ProviderError("回答包含不存在的来源编号，未提交该回答。")
        return "[" + identifiers[index] + "]"

    answer = re.sub(r"[\[（(〔]S(\d+)[\]）)〕]", handle, answer)
    return re.sub(
        r"[\[〔](?:(?:来源(?:ID)?|证据(?:ID)?)\s*[:：]?\s*)?([0-9a-f-]{8,36})[\]〕]",
        expand,
        answer,
    )


def model_evidence(evidence):
    """Expose only task-local handles; database identifiers stay server-side."""
    return [
        {
            "source": f"S{i}",
            **{k: item[k] for k in ("text", "document", "section", "excerpt_truncated") if k in item},
        }
        for i, item in enumerate(evidence, 1)
    ]

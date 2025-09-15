import re


def get_json_str(raw_output: str) -> str:
    # Try to find a fenced JSON block first
    match = re.search(r"```json\s*(\{[\s\S]*\})\s*```", raw_output)
    if match:
        json_str = match.group(1)
    else:
        # Fallback: extract first {…} JSON substring
        start = raw_output.find("{")
        end = raw_output.rfind("}") + 1
        if start != -1 and end != -1:
            json_str = raw_output[start:end]
        else:
            return raw_output.strip()
    return json_str.strip()

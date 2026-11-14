/** Decode terminal control sequences across arbitrary output chunk boundaries. */
export function loginText() {
  let state: "text" | "escape" | "csi" | "osc" | "stringEscape" | "intermediate" = "text";
  return (chunk: string) => {
    let text = "";
    for (const character of chunk) {
      const code = character.charCodeAt(0);
      if (state === "osc") {
        if (code === 7 || code === 156) state = "text";
        else if (code === 27) state = "stringEscape";
      } else if (state === "stringEscape") {
        state = character === "\\" ? "text" : code === 27 ? "stringEscape" : "osc";
      } else if (code === 27) state = "escape";
      else if (state === "escape") {
        if (character === "[") state = "csi";
        else if (["]", "P", "X", "^", "_"].includes(character)) state = "osc";
        else state = code >= 32 && code <= 47 ? "intermediate" : "text";
      } else if (state === "csi") {
        if (code >= 64 && code <= 126) state = "text";
      } else if (state === "intermediate") {
        if (code >= 48 && code <= 126) state = "text";
      } else if (code === 155) state = "csi";
      else if (code === 157) state = "osc";
      else if (
        character === "\n" ||
        character === "\t" ||
        (code >= 32 && !(code >= 127 && code <= 159))
      )
        text += character;
    }
    return text;
  };
}

import { markdown } from "@codemirror/lang-markdown";
import { python } from "@codemirror/lang-python";
import { yaml } from "@codemirror/lang-yaml";
import { StreamLanguage } from "@codemirror/language";
import { shell } from "@codemirror/legacy-modes/mode/shell";
import { Compartment, EditorState, type Extension } from "@codemirror/state";
import { oneDark } from "@codemirror/theme-one-dark";
import { EditorView, keymap } from "@codemirror/view";
import { useComputedColorScheme } from "@mantine/core";
import { basicSetup } from "codemirror";
import { useEffect, useRef } from "react";

import type { CommandFileFormat } from "../api/client";

const languageCompartment = new Compartment();
const themeCompartment = new Compartment();
const readOnlyCompartment = new Compartment();

function languageExtension(format: CommandFileFormat): Extension {
  switch (format) {
    case "markdown":
      return markdown();
    case "python":
      return python();
    case "yaml":
      return yaml();
    case "shell":
      return StreamLanguage.define(shell);
  }
}

function themeExtension(dark: boolean): Extension {
  return dark ? oneDark : [];
}

export type CommandEditorProps = {
  value: string;
  format: CommandFileFormat;
  disabled?: boolean;
  onChange: (value: string) => void;
  onSave: () => void;
};

export function CommandEditor({
  value,
  format,
  disabled = false,
  onChange,
  onSave,
}: CommandEditorProps) {
  const colorScheme = useComputedColorScheme("light");
  const dark = colorScheme === "dark";
  const containerRef = useRef<HTMLDivElement | null>(null);
  const viewRef = useRef<EditorView | null>(null);

  // Keep the latest callbacks in refs so the editor is created once and never
  // torn down on every render (which would drop focus and IME composition).
  const onChangeRef = useRef(onChange);
  const onSaveRef = useRef(onSave);
  useEffect(() => {
    onChangeRef.current = onChange;
    onSaveRef.current = onSave;
  });

  useEffect(() => {
    if (!containerRef.current) {
      return;
    }
    const saveKeymap = keymap.of([
      {
        key: "Mod-s",
        preventDefault: true,
        run: () => {
          onSaveRef.current();
          return true;
        },
      },
    ]);
    const updateListener = EditorView.updateListener.of((update) => {
      if (update.docChanged) {
        onChangeRef.current(update.state.doc.toString());
      }
    });
    const view = new EditorView({
      parent: containerRef.current,
      state: EditorState.create({
        doc: value,
        extensions: [
          basicSetup,
          saveKeymap,
          updateListener,
          languageCompartment.of(languageExtension(format)),
          themeCompartment.of(themeExtension(dark)),
          readOnlyCompartment.of(EditorState.readOnly.of(disabled)),
        ],
      }),
    });
    viewRef.current = view;
    return () => {
      view.destroy();
      viewRef.current = null;
    };
    // Editor instance is created once; content/format/theme are synced below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Sync external content changes (file switch, revert, reload) without
  // clobbering the cursor during the user's own typing.
  useEffect(() => {
    const view = viewRef.current;
    if (view && value !== view.state.doc.toString()) {
      view.dispatch({
        changes: { from: 0, to: view.state.doc.length, insert: value },
      });
    }
  }, [value]);

  useEffect(() => {
    viewRef.current?.dispatch({
      effects: languageCompartment.reconfigure(languageExtension(format)),
    });
  }, [format]);

  useEffect(() => {
    viewRef.current?.dispatch({
      effects: themeCompartment.reconfigure(themeExtension(dark)),
    });
  }, [dark]);

  useEffect(() => {
    viewRef.current?.dispatch({
      effects: readOnlyCompartment.reconfigure(EditorState.readOnly.of(disabled)),
    });
  }, [disabled]);

  return <div ref={containerRef} className="command-editor-surface" data-testid="command-editor" />;
}

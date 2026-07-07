/// <reference types="vite/client" />

import { DefaultMantineColor, MantineColorsTuple } from "@mantine/core";

type ExtendedCustomColors = "neutral" | "success" | "warning" | "danger" | "info";

declare module "@mantine/core" {
  export interface MantineThemeColorsOverride {
    colors: Record<ExtendedCustomColors, MantineColorsTuple> &
      Record<DefaultMantineColor, MantineColorsTuple>;
  }
}

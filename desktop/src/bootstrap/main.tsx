import React from "react";
import ReactDOM from "react-dom/client";
import { MantineProvider } from "@mantine/core";

import { Bootstrap } from "../Bootstrap";
import { theme } from "../theme";
import "../i18n";
import "@mantine/core/styles.css";
import "../styles.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <MantineProvider theme={theme} defaultColorScheme="auto">
      <Bootstrap />
    </MantineProvider>
  </React.StrictMode>,
);

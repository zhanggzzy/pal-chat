import React, { Profiler } from "react";
import ReactDOM from "react-dom/client";

import { App } from "./App";
import { isUiPerfTraceEnabled, recordReactProfilerCommit } from "./perfTrace";

const app = isUiPerfTraceEnabled() ? (
  <Profiler id="app-root" onRender={recordReactProfilerCommit}>
    <App />
  </Profiler>
) : (
  <App />
);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>{app}</React.StrictMode>,
);

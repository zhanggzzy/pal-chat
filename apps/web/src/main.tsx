import React, { Profiler } from "react";
import ReactDOM from "react-dom/client";

import { App } from "./App";
import { recordReactProfilerCommit } from "./perfTrace";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <Profiler id="app-root" onRender={recordReactProfilerCommit}>
      <App />
    </Profiler>
  </React.StrictMode>,
);

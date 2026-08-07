import ReactDOM from "react-dom/client";
import App from "./App";
import "./index.css";

// No StrictMode: its dev-only double effect invocation would fire the mount
// agentStatus twice, racing two server spawns (the Rust ensure() is now
// serialized, but there is nothing to gain from the double-render here).
ReactDOM.createRoot(document.getElementById("root")!).render(<App />);

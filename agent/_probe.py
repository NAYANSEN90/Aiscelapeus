import inspect
import livekit.agents as A
print("VER", getattr(A, "__version__", "?"))
names = [n for n in dir(A) if not n.startswith("_")]
print("EXPORTS", names)
for n in ["AgentServer", "AgentSession", "Agent", "JobContext", "function_tool", "RunContext", "cli", "inference", "room_io"]:
    print("HAS", n, hasattr(A, n))
if hasattr(A, "AgentServer"):
    S = A.AgentServer
    print("AgentServer init", inspect.signature(S.__init__))
    print("AgentServer methods", [m for m in dir(S) if not m.startswith("_")])
print("AgentSession init", inspect.signature(A.AgentSession.__init__))
print("Agent init", inspect.signature(A.Agent.__init__))
print("session.start", inspect.signature(A.AgentSession.start))

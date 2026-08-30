import os

import uvicorn

if __name__ == "__main__":
    # Defaults suit the container (bind all interfaces); Start.bat overrides HOST
    # to 127.0.0.1 so a local install is not reachable from the rest of the network.
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("backend.main:app", host=host, port=port, reload=False)

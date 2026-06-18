const http = require("http");
const fs = require("fs");
const path = require("path");

const projectRoot = path.resolve(__dirname, "..");
const staticRoot = path.join(projectRoot, "src", "copilot_api", "static");

function loadEnvFile() {
  const envPath = path.join(projectRoot, ".env");
  if (!fs.existsSync(envPath)) return;

  for (const rawLine of fs.readFileSync(envPath, "utf8").split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#") || !line.includes("=")) continue;
    const separator = line.indexOf("=");
    const key = line.slice(0, separator).trim();
    const value = line
      .slice(separator + 1)
      .trim()
      .replace(/^(['"])(.*)\1$/, "$2");
    if (key && process.env[key] === undefined) process.env[key] = value;
  }
}

loadEnvFile();

const frontendHost = process.env.FRONTEND_HOST || "127.0.0.1";
const frontendPort = Number(process.env.FRONTEND_PORT || 3000);
const backendHost = process.env.BACKEND_HOST || "127.0.0.1";
const backendPort = Number(process.env.BACKEND_PORT || 8000);

const contentTypes = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".webmanifest": "application/manifest+json; charset=utf-8",
  ".png": "image/png",
  ".svg": "image/svg+xml",
};

function sendFile(response, filePath) {
  fs.readFile(filePath, (error, content) => {
    if (error) {
      response.writeHead(error.code === "ENOENT" ? 404 : 500);
      response.end(error.code === "ENOENT" ? "Not found" : "Unable to read file");
      return;
    }

    response.writeHead(200, {
      "Content-Type": contentTypes[path.extname(filePath)] || "application/octet-stream",
      "Cache-Control": "no-cache",
    });
    response.end(content);
  });
}

function proxyToBackend(request, response) {
  const headers = {
    ...request.headers,
    host: `${backendHost}:${backendPort}`,
    "x-forwarded-host": request.headers.host,
    "x-forwarded-proto": "http",
  };
  delete headers.connection;

  const proxyRequest = http.request(
    {
      hostname: backendHost,
      port: backendPort,
      path: request.url,
      method: request.method,
      headers,
    },
    (proxyResponse) => {
      response.writeHead(proxyResponse.statusCode || 502, proxyResponse.headers);
      proxyResponse.pipe(response);
    }
  );

  proxyRequest.on("error", () => {
    response.writeHead(502, { "Content-Type": "application/json; charset=utf-8" });
    response.end(
      JSON.stringify({
        detail: `Backend is not running. Start it with: python main.py`,
      })
    );
  });

  request.pipe(proxyRequest);
}

const server = http.createServer((request, response) => {
  const requestUrl = new URL(request.url, `http://${request.headers.host}`);

  if (requestUrl.pathname === "/") {
    sendFile(response, path.join(staticRoot, "index.html"));
    return;
  }

  if (requestUrl.pathname.startsWith("/static/")) {
    const relativePath = requestUrl.pathname.slice("/static/".length);
    const filePath = path.resolve(staticRoot, relativePath);
    if (!filePath.startsWith(`${staticRoot}${path.sep}`)) {
      response.writeHead(403);
      response.end("Forbidden");
      return;
    }
    sendFile(response, filePath);
    return;
  }

  if (requestUrl.pathname === "/favicon.ico") {
    sendFile(response, path.join(staticRoot, "favicon.svg"));
    return;
  }

  proxyToBackend(request, response);
});

server.listen(frontendPort, frontendHost, () => {
  console.log(`Frontend: http://${frontendHost}:${frontendPort}`);
  console.log(`Proxying API requests to http://${backendHost}:${backendPort}`);
});

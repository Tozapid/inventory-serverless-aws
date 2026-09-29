function handler(event) {
  var request = event.request;
  var host = request.headers.host && request.headers.host.value;
  if (host) {
    request.headers["x-viewer-host"] = {value: host};
  }
  return request;
}

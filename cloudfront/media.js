function handler(event) {
  var request = event.request;
  if (request.uri.indexOf("/media/") === 0) {
    request.uri = request.uri.substring(6);
  }
  return request;
}

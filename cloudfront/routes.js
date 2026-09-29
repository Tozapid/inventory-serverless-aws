function handler(event) {
  var uri = event.request.uri;
  // Card links are /box/<number>. The shell is a single page, so those
  // paths must return index.html. A dot in the number must not look like a file.
  if (uri === "/box" || uri.indexOf("/box/") === 0) {
    event.request.uri = "/index.html";
  }
  return event.request;
}

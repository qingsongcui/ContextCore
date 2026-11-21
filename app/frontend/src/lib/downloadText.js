export function downloadText(filename, body) {
  const url = URL.createObjectURL(new Blob([body], { type: 'text/plain;charset=utf-8' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  try {
    link.click();
  } finally {
    link.remove();
    // Keep the blob available while WebKit hands the download to its save panel.
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  }
}

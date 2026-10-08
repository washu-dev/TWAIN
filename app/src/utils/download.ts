import { Platform } from 'react-native';

/**
 * Save a Blob as a file. Web only (the deployment target): an object URL and a
 * temporary <a download>, so files fetched with the bearer token -- which a plain
 * link can't carry -- still download. Returns false where saving isn't supported.
 */
export function saveBlob(blob: Blob, filename: string): boolean {
  if (Platform.OS !== 'web' || typeof document === 'undefined') return false;
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  return true;
}

/** A text file's contents as a download named after the last path segment. */
export function saveText(text: string, path: string): boolean {
  const name = path.split('/').pop() || 'file.txt';
  return saveBlob(new Blob([text], { type: 'text/plain;charset=utf-8' }), name);
}

export const canDownload = Platform.OS === 'web';

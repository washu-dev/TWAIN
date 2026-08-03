import { Platform } from 'react-native';

/**
 * Whether copying is possible here.
 *
 * Web only: the app ships as an Expo web build (S3 + CloudFront), and no
 * clipboard module is installed for native — react-native's own `Clipboard` was
 * deprecated and moved out of core. Callers use this to hide the affordance
 * rather than offer a button that silently does nothing.
 */
export const canCopy = Platform.OS === 'web';

/**
 * Put `text` on the clipboard. Resolves true on success.
 *
 * Prefers the async Clipboard API, which needs a secure context — http://localhost
 * counts, but a plain-http LAN address does not, and TWAIN gets opened that way
 * during demos. The hidden-textarea fallback covers that case.
 */
export async function copyText(text: string): Promise<boolean> {
  if (!canCopy || !text) return false;

  const nav = globalThis.navigator as Navigator | undefined;
  if (nav?.clipboard?.writeText) {
    try {
      await nav.clipboard.writeText(text);
      return true;
    } catch {
      // Denied permission or a non-secure context — fall through.
    }
  }

  const doc = globalThis.document as Document | undefined;
  if (!doc?.body) return false;
  const scratch = doc.createElement('textarea');
  scratch.value = text;
  // Off-screen but still focusable: display:none or hidden would make the
  // selection (and therefore the copy) fail.
  scratch.setAttribute('readonly', '');
  scratch.style.position = 'fixed';
  scratch.style.top = '-1000px';
  scratch.style.opacity = '0';
  doc.body.appendChild(scratch);
  try {
    scratch.select();
    return doc.execCommand('copy');
  } catch {
    return false;
  } finally {
    doc.body.removeChild(scratch);
  }
}

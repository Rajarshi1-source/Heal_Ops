/**
 * Build-time switches.
 *
 * `READ_ONLY` is inlined at build time, so a read-only build cannot render a write control at all:
 * the approval buttons are absent from the bundle, not hidden or unwired. An approval control that
 * 404s invites someone to find out why. It fails closed -- anything but an explicit "false" is
 * read-only.
 */
export const READ_ONLY = process.env.NEXT_PUBLIC_READ_ONLY !== "false";

import type { ReactNode } from 'react'

/**
 * Row actions are icons rather than words: a table line carries several of them,
 * and spelled-out verbs push the data itself off the screen. The word survives
 * as the tooltip and as the accessible name, so nothing is lost to a reader who
 * hovers, tabs, or listens instead of recognising the glyph.
 */
export type IconName =
  | 'show' | 'hide' | 'edit' | 'save' | 'cancel' | 'delete' | 'forget' | 'set'
  | 'default' | 'automatic' | 'up' | 'down' | 'expand' | 'collapse'
  | 'test' | 'disconnect' | 'open'

const PATHS: Record<IconName, ReactNode> = {
  show: <>
    <path d="M1.6 8C4 4.4 12 4.4 14.4 8 12 11.6 4 11.6 1.6 8Z" />
    <circle cx="8" cy="8" r="2" />
  </>,
  hide: <>
    <path d="M1.6 8C4 4.4 12 4.4 14.4 8 12 11.6 4 11.6 1.6 8Z" />
    <path d="M2.8 2.8l10.4 10.4" />
  </>,
  edit: <>
    <path d="M11.3 2.4a1.3 1.3 0 0 1 1.9 0l.4.4a1.3 1.3 0 0 1 0 1.9L6.3 12 3.5 12.5 4 9.7Z" />
    <path d="M10.2 3.6l2.2 2.2" />
  </>,
  save: <path d="M3.2 8.4l3.2 3.2 6.4-7.2" />,
  cancel: <><path d="M4 4l8 8" /><path d="M12 4l-8 8" /></>,
  delete: <>
    <path d="M3 4.3h10" />
    <path d="M6.4 4.3V2.9h3.2v1.4" />
    <path d="M4.6 4.3l.5 8.3a.8.8 0 0 0 .8.7h4.2a.8.8 0 0 0 .8-.7l.5-8.3" />
  </>,
  forget: <>
    <path d="M3 4.3h10" />
    <path d="M6.4 4.3V2.9h3.2v1.4" />
    <path d="M4.6 4.3l.5 8.3a.8.8 0 0 0 .8.7h4.2a.8.8 0 0 0 .8-.7l.5-8.3" />
  </>,
  set: <>
    <rect x="3.2" y="7" width="9.6" height="6.2" rx="1.2" />
    <path d="M5.6 7V5.2a2.4 2.4 0 0 1 4.8 0V7" />
  </>,
  default: <path d="M8 2.4l1.75 3.5 3.85.55-2.8 2.7.66 3.85L8 11.2l-3.46 1.8.66-3.85-2.8-2.7 3.85-.55Z" />,
  automatic: <>
    <path d="M2 8a6 6 0 1 0 6-6 6.5 6.5 0 0 0-4.5 1.9L2 5.4" />
    <path d="M2 2v3.4h3.4" />
  </>,
  up: <path d="M4 9.8L8 5.8l4 4" />,
  down: <path d="M4 6.2l4 4 4-4" />,
  expand: <path d="M6 3.2l4.6 4.8L6 12.8" />,
  collapse: <path d="M3.2 6l4.8 4.6L12.8 6" />,
  test: <path d="M1.6 8h2.9l2-4.6 3 9.2 2-4.6h2.9" />,
  disconnect: <>
    <path d="M8 2.4v5" />
    <path d="M4.6 4.6a4.8 4.8 0 1 0 6.8 0" />
  </>,
  open: <>
    <path d="M9.2 2.6h4.2v4.2" />
    <path d="M13.4 2.6L7.6 8.4" />
    <path d="M12 9.4v3a1.2 1.2 0 0 1-1.2 1.2H3.6a1.2 1.2 0 0 1-1.2-1.2V5.2A1.2 1.2 0 0 1 3.6 4h3" />
  </>,
}

export function Icon({ name }: { name: IconName }) {
  return (
    <svg
      className="icon" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"
      fill="none" stroke="currentColor" strokeWidth="1.4"
      strokeLinecap="round" strokeLinejoin="round"
    >
      {PATHS[name]}
    </svg>
  )
}

type Common = {
  /** The word the button used to spell out — kept as tooltip and screen-reader name. */
  label: string
  icon: IconName
  /** Longer tooltip when the bare verb needs the extra context the text carried. */
  title?: string
  danger?: boolean
}

export default function IconButton({
  label, icon, title, danger, type = 'button', disabled, onClick,
}: Common & {
  type?: 'button' | 'submit'
  disabled?: boolean
  onClick?: () => void
}) {
  return (
    <button
      type={type}
      className={`icon-btn${danger ? ' danger' : ''}`}
      title={title ?? label}
      aria-label={label}
      disabled={disabled}
      onClick={onClick}
    >
      <Icon name={icon} />
    </button>
  )
}

/** The same affordance for actions that are genuinely a link, such as opening a PDF. */
export function IconLink({
  label, icon, title, href, target, rel,
}: Common & { href: string; target?: string; rel?: string }) {
  return (
    <a
      className="icon-btn" href={href} target={target} rel={rel}
      title={title ?? label} aria-label={label}
    >
      <Icon name={icon} />
    </a>
  )
}

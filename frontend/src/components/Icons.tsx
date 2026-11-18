import React from 'react';

interface IconProps {
  size?: number;
}

const baseProps = (size = 16) => ({
  width: size,
  height: size,
  viewBox: '0 0 24 24',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.75,
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
  'aria-hidden': true,
});

/* Top Bar Navigation Icons */
export const IconBriefcase: React.FC<IconProps> = ({ size = 17 }) => (
  <svg {...baseProps(size)}>
    <rect x="3" y="7" width="18" height="13" rx="2" />
    <path d="M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
    <path d="M3 12h18" />
  </svg>
);

export const IconStudioDoc: React.FC<IconProps> = ({ size = 17 }) => (
  <svg {...baseProps(size)}>
    <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-7" />
    <path d="M9 13h6M9 17h4" />
    <path d="m18.5 2.5 1 2.5 2.5 1-2.5 1-1 2.5-1-2.5-2.5-1 2.5-1 1-2.5Z" />
  </svg>
);

export const IconBoardCheck: React.FC<IconProps> = ({ size = 17 }) => (
  <svg {...baseProps(size)}>
    <rect x="3" y="4" width="18" height="16" rx="2" />
    <path d="M9 4v16" />
    <path d="m12.5 12 2 2 4-4" />
  </svg>
);

export const IconUserDoc: React.FC<IconProps> = ({ size = 17 }) => (
  <svg {...baseProps(size)}>
    <rect x="4" y="3" width="16" height="18" rx="2" />
    <circle cx="12" cy="10" r="2.5" />
    <path d="M8.5 16.5c.9-1.6 2.1-2.3 3.5-2.3s2.6.7 3.5 2.3" />
  </svg>
);

/* Functional Action Icons */
export const IconSearch: React.FC<IconProps> = ({ size }) => (
  <svg {...baseProps(size)}>
    <circle cx="11" cy="11" r="7" />
    <path d="m20 20-3.5-3.5" />
  </svg>
);

export const IconPlus: React.FC<IconProps> = ({ size }) => (
  <svg {...baseProps(size)}>
    <path d="M12 5v14M5 12h14" />
  </svg>
);

export const IconTrash: React.FC<IconProps> = ({ size }) => (
  <svg {...baseProps(size)}>
    <path d="M4 7h16M9 7V4h6v3M7 7l1 13h8l1-13" />
  </svg>
);

export const IconGear: React.FC<IconProps> = ({ size }) => (
  <svg {...baseProps(size)}>
    <circle cx="12" cy="12" r="3" />
    <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h.08a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h.08a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v.08a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1Z" />
  </svg>
);

export const IconUpload: React.FC<IconProps> = ({ size }) => (
  <svg {...baseProps(size)}>
    <path d="M12 16V4m0 0-4 4m4-4 4 4" />
    <path d="M4 17v2a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-2" />
  </svg>
);

export const IconDownload: React.FC<IconProps> = ({ size }) => (
  <svg {...baseProps(size)}>
    <path d="M12 4v12m0 0-4-4m4 4 4-4" />
    <path d="M4 17v2a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-2" />
  </svg>
);

export const IconChevronDown: React.FC<IconProps> = ({ size }) => (
  <svg {...baseProps(size)}>
    <path d="m6 9 6 6 6-6" />
  </svg>
);

export const IconChevronRight: React.FC<IconProps> = ({ size }) => (
  <svg {...baseProps(size)}>
    <path d="m9 6 6 6-6 6" />
  </svg>
);

export const IconArrowUp: React.FC<IconProps> = ({ size }) => (
  <svg {...baseProps(size)}>
    <path d="M12 19V5m0 0-5 5m5-5 5 5" />
  </svg>
);

export const IconArrowDown: React.FC<IconProps> = ({ size }) => (
  <svg {...baseProps(size)}>
    <path d="M12 5v14m0 0-5-5m5 5 5-5" />
  </svg>
);

export const IconExternal: React.FC<IconProps> = ({ size }) => (
  <svg {...baseProps(size)}>
    <path d="M14 4h6v6M20 4l-9 9" />
    <path d="M10 6H5a1 1 0 0 0-1 1v12a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-5" />
  </svg>
);

export const IconClose: React.FC<IconProps> = ({ size }) => (
  <svg {...baseProps(size)}>
    <path d="M18 6 6 18M6 6l12 12" />
  </svg>
);

export const IconSparkles: React.FC<IconProps> = ({ size }) => (
  <svg {...baseProps(size)}>
    <path d="m12 3 1.8 4.7L18.5 9.5l-4.7 1.8L12 16l-1.8-4.7L5.5 9.5l4.7-1.8L12 3Z" />
    <path d="M19 16v4M17 18h4" />
  </svg>
);

export const IconFile: React.FC<IconProps> = ({ size }) => (
  <svg {...baseProps(size)}>
    <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5Z" />
    <path d="M14 3v5h5" />
  </svg>
);

export const IconCheck: React.FC<IconProps> = ({ size }) => (
  <svg {...baseProps(size)}>
    <path d="m5 12 4.5 4.5L19 7" />
  </svg>
);

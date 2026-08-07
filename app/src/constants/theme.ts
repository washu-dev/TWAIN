/**
 * WashU Theme and App Configuration
 * Colors sourced from https://marcomm.washu.edu/brand-color-palette/
 */

import { Platform } from 'react-native';

/**
 * The palette. This is the ONLY place a colour may be written down.
 *
 * It grew three roles it did not name -- structural greys (borders, dividers), a
 * text ramp above `textSecondary`, and semantic status colours -- so screens
 * reached for literals instead: 141 of them, including 23 copies of the red that
 * was already here and 13 copies of one border grey. Everything below is a value
 * that was already shipping; naming it is what makes a restyle a change to this
 * file rather than a sweep through fifteen.
 *
 * NOTE ON `dark`: nothing reaches it today. Every screen pins `Colors.light` at
 * module scope and `useColorScheme()` returns 'light' unconditionally on web, so
 * the dark column is a structural placeholder, not a designed or tested theme.
 * Making dark mode real is its own piece of work.
 */
export const Colors = {
  light: {
    text: '#000000',
    background: '#ffffff',
    backgroundElement: '#F5F0F0',
    backgroundSelected: '#E8D5D5',
    textSecondary: '#5A5A5A',
    washuRed: '#BA0C2F',       // Primary — Pantone 200
    washuGreen: '#215732',     // Secondary — Pantone 350
    washuWhite: '#FFFFFF',
    washuLightGray: '#F2F2F2',

    // The page canvas. Deliberately NOT washuLightGray (#F2F2F2): a flat cool
    // grey behind white cards is what makes an interface read as a form, because
    // the two surfaces have no relationship. This is a warm near-white a couple
    // of degrees off the card white, so cards sit ON something rather than being
    // holes cut in grey. Brand tokens keep their exact values -- this is a new
    // name, not a redefinition of WashU's palette.
    canvas: '#FAF8F8',
    washuDarkGray: '#333333',

    // Structure. `border` is the default 1px rule; `borderStrong` is for the few
    // places that need more definition; `divider` separates sections inside a
    // surface (modal footers, list rows).
    border: '#DDDDDD',
    borderStrong: '#CCCCCC',
    divider: '#EEEEEE',

    // Text ramp, darkest first. `textStrong` is heading black -- deliberately not
    // `text` (#000000), which nothing uses for body copy.
    textStrong: '#1A1A1A',
    textPlaceholder: '#9A9A9A',

    // Surfaces that are dark in BOTH themes (the site footer), and the text on
    // them. Same values as the dark theme's, but a footer is not "dark mode" --
    // giving it its own name stops the two ideas from being confused.
    surfaceDark: '#1A1A1A',
    textOnDark: '#FFFFFF',
    textOnDarkMuted: '#AAAAAA',

    // Status. Success and failure are the brand green and red (used directly);
    // these are the two states the brand palette has no colour for, plus the
    // tinted surface that pairs with `washuRed`.
    warning: '#B8860B',
    info: '#0B69C7',
    errorSurface: '#FDF3F4',

    shadow: '#000000',
  },
  dark: {
    text: '#ffffff',
    background: '#1A1A1A',
    backgroundElement: '#2A2A2A',
    backgroundSelected: '#3A3A3A',
    textSecondary: '#AAAAAA',
    washuRed: '#BA0C2F',
    washuGreen: '#215732',
    washuWhite: '#FFFFFF',
    washuLightGray: '#2A2A2A',
    washuDarkGray: '#CCCCCC',

    border: '#3A3A3A',
    borderStrong: '#4A4A4A',
    divider: '#2A2A2A',

    textStrong: '#FFFFFF',
    textPlaceholder: '#777777',

    surfaceDark: '#1A1A1A',
    textOnDark: '#FFFFFF',
    textOnDarkMuted: '#AAAAAA',

    warning: '#E0A72E',
    info: '#5AA9F0',
    errorSurface: '#3A1F22',

    shadow: '#000000',
  },
} as const;

export type ThemeColor = keyof typeof Colors.light & keyof typeof Colors.dark;

export const Fonts = Platform.select({
  ios: {
    sans: 'system-ui',
    serif: 'ui-serif',
    rounded: 'ui-rounded',
    mono: 'ui-monospace',
  },
  default: {
    sans: 'normal',
    serif: 'serif',
    rounded: 'normal',
    mono: 'monospace',
  },
  web: {
    sans: 'var(--font-display)',
    serif: 'var(--font-serif)',
    rounded: 'var(--font-rounded)',
    mono: 'var(--font-mono)',
  },
});

/**
 * What a tile's accent stripe MEANS, so the colour carries information instead of
 * decorating. The homepage used to alternate red/green/green/red/green with no
 * rule behind it, which made the page look designed but told a reader nothing.
 *
 *   spends -- starts a run, or posts something outside TWAIN. Consequences.
 *   reads  -- read-only. Nothing happens to anything by opening it.
 *
 * There is deliberately no third colour: everything else is minor enough to be an
 * IconTile, which carries no stripe at all. A token for a case nothing uses would
 * be vocabulary pretending to be a rule.
 */
/**
 * The visual language layered over the brand palette: gradients, depth, motion.
 *
 * Kept as tokens rather than inline values so "make it feel more premium" is a
 * change in one file instead of forty call sites -- and so the restraint is
 * enforceable. Three rules hold this together:
 *
 *   1. WashU red stays THE accent. Saturation comes from using it deliberately
 *      against desaturated surfaces, not from adding new hues. A university
 *      product that invents its own palette stops looking like the university's.
 *   2. Gradients are for AMBIENCE, not decoration -- large, low-contrast washes
 *      behind content. A gradient the eye can resolve into two colours reads as
 *      a 2010s button; one it reads as "slightly warm here" reads as depth.
 *   3. Shadows are layered and tinted with the brand ink, never neutral black at
 *      high opacity. Two soft layers (a tight contact shadow plus a wide
 *      diffuse one) is what separates "raised" from "outlined".
 */
export const Gradients = {
  // The masthead. Deep crimson into near-black plum, so the white wordmark sits
  // on the darkest corner and the light edge falls where content begins.
  brandHeader: ['#8E0A24', '#BA0C2F', '#6E0A2A'] as const,
  brandHeaderLocations: [0, 0.55, 1] as const,
  // The primary action. Lifts on the diagonal so the arrow end is brightest.
  action: ['#C8102E', '#A00C28'] as const,
  // Read-only surfaces: the brand green, kept quiet.
  calm: ['#27633A', '#1B4A2A'] as const,
  // The page wash. Barely-there warm tint at the top of a scroll, fading out --
  // this is the "ambiance", and it is deliberately almost invisible: at these
  // opacities it reads as light in the room rather than as a coloured panel.
  ambient: ['rgba(186,12,47,0.085)', 'rgba(186,12,47,0.030)', 'rgba(250,248,248,0)'] as const,
  ambientLocations: [0, 0.5, 1] as const,
  // Glass-ish overlay for a bar sitting over content. Not a real blur: web has
  // no backdrop-filter through react-native-web, so this is an honest scrim.
  scrim: ['rgba(255,255,255,0)', 'rgba(255,255,255,0.86)', 'rgba(255,255,255,0.98)'] as const,
} as const;

/**
 * Layered elevation. `card` is the resting state, `raised` the hover/press
 * response, `hero` the one element per screen that should feel closest.
 *
 * Shadows are tinted with the brand ink (a plum-black) rather than pure black:
 * neutral black over a warm-white surface reads as grey dirt, a tinted shadow
 * reads as depth. Values are RN 0.85 `boxShadow`, which react-native-web
 * forwards to CSS -- so one declaration covers native and web.
 */
export const Elevation = {
  card: '0px 1px 2px rgba(26,6,12,0.05), 0px 4px 12px rgba(26,6,12,0.045)',
  raised: '0px 2px 4px rgba(26,6,12,0.07), 0px 10px 24px rgba(26,6,12,0.075)',
  hero: '0px 2px 6px rgba(26,6,12,0.08), 0px 16px 40px rgba(26,6,12,0.10)',
  // For something sitting ON the dark header.
  onDark: '0px 8px 24px rgba(0,0,0,0.28)',
} as const;

/**
 * Motion. One vocabulary, so nothing on screen moves at a speed nothing else
 * does -- inconsistent easing is the single most common tell of an interface
 * assembled from parts.
 *
 * Durations are short on purpose: "smooth" is not "slow". Anything over ~350ms
 * on a tap feels like latency, and this app already makes people wait for real
 * computation, so the interface must never add to that impression.
 */
export const Motion = {
  instant: 90,     // press feedback
  quick: 180,      // state change on a control
  smooth: 280,     // element entrance, screen content
  page: 320,       // screen transition
  // Standard ease-out: fast start, settled end. A CSS/Easing.bezier cubic-bezier.
  easeOut: [0.22, 1, 0.36, 1] as const,
  // Gentle spring for press release. Spelled for Animated.spring's physics config,
  // which takes the same three parameters with the same meanings as Reanimated's
  // withSpring -- so the feel is portable if this ever moves back.
  spring: { damping: 18, stiffness: 220, mass: 0.6 } as const,
  // How much a pressable shrinks. 0.97 is felt but not cartoonish.
  pressScale: 0.97,
  // Delay between staggered siblings. Small: a list that cascades for a second
  // is a demo, not a product.
  stagger: 45,
  // Behind a modal. Tinted with the brand ink rather than pure black: a neutral
  // black wash over a warm canvas greys everything behind it, where a plum-tinted
  // one dims the page while leaving it recognisably the same page. Four modals
  // hardcoded their own rgba(0,0,0,…) at two different opacities before this.
  scrimColor: 'rgba(26,6,12,0.52)',
} as const;

/** Radii. Larger than the old 6px, which is what dates an interface fastest. */
export const Radius = {
  control: 10,
  card: 14,
  hero: 18,
  pill: 999,
} as const;

export const TileAccent = {
  spends: Colors.light.washuRed,
  reads: Colors.light.washuGreen,
} as const;

export const Spacing = {
  half: 2,
  one: 4,
  two: 8,
  three: 16,
  four: 24,
  five: 32,
  six: 64,
} as const;

export const BottomTabInset = Platform.select({ ios: 50, android: 80 }) ?? 0;
export const MaxContentWidth = 800;

export const API_CONFIG = {
  baseURL: process.env.EXPO_PUBLIC_API_BASE_URL || 'http://localhost:8000',
  timeout: 10000,
};

/**
 * Microsoft Entra ID (Azure AD) SSO config for the OIDC auth-code + PKCE flow.
 * Values come from EXPO_PUBLIC_* env vars (inlined by Expo at build time); see
 * app/.env.example. The tenant defaults to WashU's, so only the SPA client id
 * normally needs setting (the API scope is optional — see `apiScope` below).
 * `isEntraConfigured` gates whether the app can offer SSO — when false (and auth
 * isn't disabled) the login screen says so
 * instead of launching a broken flow.
 */
const ENTRA_TENANT_ID =
  process.env.EXPO_PUBLIC_ENTRA_TENANT_ID || '4ccca3b5-71cd-4e6d-974b-4d9beb96c6d6';

export const ENTRA_CONFIG = {
  tenantId: ENTRA_TENANT_ID,
  // Application (client) ID of the "twain-web" SPA app registration (public/PKCE).
  clientId: process.env.EXPO_PUBLIC_ENTRA_CLIENT_ID || '',
  // Optional API scope to request so the *access* token's audience matches the
  // API, e.g. "api://twain-api/access_as_user". When empty, the app runs in
  // ID-token mode (see useAuth.tsx): it requests only the standard OIDC scopes
  // and sends the ID token — whose audience is this client id — as the API
  // bearer, needing no exposed API scope or admin consent.
  apiScope: process.env.EXPO_PUBLIC_ENTRA_API_SCOPE || '',
  // OIDC issuer / authority for the tenant; discovery hangs off this.
  authority: `https://login.microsoftonline.com/${ENTRA_TENANT_ID}/v2.0`,
} as const;

export const isEntraConfigured = (): boolean => Boolean(ENTRA_CONFIG.clientId);

export const APP_STRINGS = {
  appTitle: 'TWAIN',
  appSubtitle: 'The WashU AI-Assisted Platform for Simulation From Narration',
  loginButton: 'Login',
  signOutButton: 'Sign out',
  loginPrompt: 'Sign in with your WUSTL Key to continue.',
  loginBackToHome: '← Back to home',
  // Entra ID single sign-on
  ssoButton: 'Sign in with your WUSTL Key',
  ssoPreparing: 'Preparing secure sign-in…',
  ssoNotConfigured:
    'Single sign-on is not configured for this deployment. Set the EXPO_PUBLIC_ENTRA_* variables to enable it.',
  ssoFootnote:
    'You will be redirected to the Washington University login to authenticate. TWAIN never sees your password.',
  testButton: 'Test',
  apiCheck: 'Check API connection',
  apiCheckDesc: 'Confirms this app can reach the TWAIN backend',
  startSimulation: 'Start a simulation',
  browse: 'Browse runs',
  // Short enough for a compact tile. The screen it opens is titled "Libraries &
  // engines", which is what it actually lists (its two sections are Calculators
  // and Libraries); this is the label, not the description.
  libraries: 'Libraries',
  librariesScreenTitle: 'Libraries & engines',
  createIssue: 'Report an issue',
  settings: 'Settings',
  tutorial: 'How TWAIN works',
  startSimulationDesc: 'Describe what you want computed, in plain English',
  browseDesc: 'Review completed simulations or continue one in progress',
  librariesDesc: 'Engines and libraries available on this cluster, and what needs provisioning',
  createIssueDesc: 'Report a bug or request a feature on GitHub',
  tutorialDesc: 'Every feature explained, with the real screens to try',
  settingsDesc: 'Choose which email notifications you receive',
  copyrightText: '© 2024 Washington University in St. Louis. All rights reserved.',
  accessibilityStatement: 'Accessibility Statement',
  privacyPolicy: 'Privacy Policy',
};

/**
 * Copy for the public landing page (app/index.tsx → LandingScreen). Kept here so
 * the marketing content stays alongside the rest of the app's strings and can be
 * reviewed without digging through JSX.
 */
export const LANDING_CONTENT = {
  tagline: 'The WashU AI-Assisted Platform for Simulation From Narration',
  heroLead:
    'Turn a plain-language research request into a planned, executed, and validated computational run — with a human approval gate before anything is built or run.',
  heroCta: 'Sign in to get started',
  exampleLabel: 'For example, ask TWAIN to:',
  exampleQuote: '“Predict the aqueous solubility of aspirin at 25 °C.”',

  whatHeading: 'What is TWAIN?',
  whatBody:
    'TWAIN is a self-discovering, self-correcting agentic pipeline for computational science. You describe the result you want in natural language; TWAIN clarifies what it needs, finds suitable methods, proposes an execution plan for your approval, generates and runs the code — on your machine or WashU research computing — and validates the results against known baselines. Every run produces reproducible artifacts and an immutable audit trail.',

  howHeading: 'How it works',
  steps: [
    {
      title: 'Describe',
      body: 'Tell TWAIN what you want in plain language — no scripts, configs, or tool names required.',
    },
    {
      title: 'Clarify',
      body: 'TWAIN asks targeted questions until your request is unambiguous and well-scoped.',
    },
    {
      title: 'Plan & approve',
      body: 'It discovers suitable methods and synthesizes an execution plan with cost and resource estimates — then waits for your approval.',
    },
    {
      title: 'Build & run',
      body: 'On approval, TWAIN generates the code and configuration and executes the run locally or on WashU HPC.',
    },
    {
      title: 'Validate',
      body: 'Results are checked against literature baselines, with automatic diagnosis and correction when a run falls short.',
    },
  ],

  capabilitiesHeading: 'Built for reproducible research',
  capabilities: [
    {
      title: 'Natural-language intake',
      body: 'Start from a sentence, not a toolchain. TWAIN maps your intent to a structured, executable goal.',
    },
    {
      title: 'You stay in control',
      body: 'Approval gates at every critical decision — nothing is built or executed without your sign-off.',
    },
    {
      title: 'Runs on WashU computing',
      body: 'Execute locally or dispatch to WashU research computing (RIS) without leaving the conversation.',
    },
    {
      title: 'Reproducible & auditable',
      body: 'Every run captures its code, configuration, results, and a full provenance trail you can revisit.',
    },
  ],

  signInHeading: 'Sign in to continue',
  signInBody:
    'Access to TWAIN requires a Washington University account. Sign in with your WashU email to start, resume, or review simulations.',
  signInCta: 'Sign in',
} as const;

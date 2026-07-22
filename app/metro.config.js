// Learn more: https://docs.expo.dev/guides/customizing-metro/
const path = require('path');
const { getDefaultConfig } = require('expo/metro-config');

const config = getDefaultConfig(__dirname);

// Prefer package `exports` maps (default in SDK 56, set explicitly for clarity).
config.resolver.unstable_enablePackageExports = true;

// @azure/msal-common (v5) ships legacy physical `browser/` and `node/` shim
// directories whose package.json `module` field points at a path that does not
// exist (`dist/index-browser.mjs`). Metro resolves those shim directories
// instead of consulting the parent package's `exports` map, so the web bundle
// fails. Redirect the subpath specifiers to the real ESM entry points.
const msalCommonDir = path.dirname(require.resolve('@azure/msal-common/package.json'));
const msalCommonSubpaths = {
  '@azure/msal-common/browser': path.join(msalCommonDir, 'dist-browser/index-browser.mjs'),
  '@azure/msal-common/node': path.join(msalCommonDir, 'dist/index-node.mjs'),
};

const defaultResolveRequest = config.resolver.resolveRequest;
config.resolver.resolveRequest = (context, moduleName, platform) => {
  const redirected = msalCommonSubpaths[moduleName];
  if (redirected) {
    return { type: 'sourceFile', filePath: redirected };
  }
  return defaultResolveRequest
    ? defaultResolveRequest(context, moduleName, platform)
    : context.resolveRequest(context, moduleName, platform);
};

module.exports = config;

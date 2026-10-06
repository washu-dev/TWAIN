import React, { useEffect, useState } from 'react';
import { View, Text, StyleSheet } from 'react-native';
import { LinearGradient } from 'expo-linear-gradient';
import { apiClient } from '@/api/client';
import { APP_VERSION, Colors, Spacing } from '@/constants/theme';

const C = Colors.light;

export const Footer: React.FC = () => {
  // Both release versions side by side (#173): the app's, baked in at build
  // time, and the API's, as it reports itself -- so "which release am I on?"
  // has an answer on every screen, and a half-finished deploy is visible.
  const [apiVersion, setApiVersion] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    apiClient.getVersion()
      .then((v) => live && setApiVersion(v.version))
      .catch(() => live && setApiVersion('unreachable'));
    return () => { live = false; };
  }, []);

  return (
    <LinearGradient
      // Plum-black rather than a flat #1A1A1A slab. Against a warm canvas the
      // pure neutral read as a hole punched in the page; carrying a trace of the
      // brand crimson into the dark makes the footer the bottom of the same room.
      colors={['#2A1016', '#150A0D']}
      start={{ x: 0, y: 0 }}
      end={{ x: 0, y: 1 }}
      style={styles.container}
    >
      {/* A 2pt crimson hairline, down from 4pt. At half the weight it reads as a
          finishing rule; at 4pt it was a second header competing with the real
          one at the top of the screen. */}
      <LinearGradient
        colors={['#8E0A24', '#BA0C2F', '#8E0A24']}
        start={{ x: 0, y: 0 }}
        end={{ x: 1, y: 0 }}
        style={styles.topBar}
      />
      {/* The address stays -- it is institutional attribution, not filler. What
          was actually costing ~90pt of every screen was the padding around it, so
          that is what shrank: same two lines, a little over half the height. */}
      <View style={styles.content}>
        <Text style={styles.brandName}>Washington University in St. Louis</Text>
        <Text style={styles.address}>One Brookings Drive, St. Louis, MO 63130</Text>
        <Text style={styles.version} accessibilityLabel={`App version ${APP_VERSION}, API version ${apiVersion ?? 'loading'}`}>
          {`App ${APP_VERSION} · API ${apiVersion ?? '…'}`}
        </Text>
      </View>
    </LinearGradient>
  );
};

const styles = StyleSheet.create({
  container: {
    marginTop: Spacing.four,
  },
  topBar: {
    height: 2,
  },
  content: {
    paddingHorizontal: Spacing.four,
    paddingTop: Spacing.two,
    // A little more underneath than above, so the last line never sits flush
    // against a phone's home indicator.
    paddingBottom: Spacing.three,
    alignItems: 'center',
    gap: Spacing.half,
  },
  brandName: {
    color: C.textOnDark,
    fontSize: 13,
    fontWeight: '600',
    textAlign: 'center',
    letterSpacing: 0.2,
  },
  version: {
    color: 'rgba(255,255,255,0.42)',
    fontSize: 11,
    textAlign: 'center',
    fontVariant: ['tabular-nums'],
  },
  address: {
    // Warmed slightly off the neutral grey so it belongs to the tinted dark it
    // sits on rather than looking like text from another palette.
    color: 'rgba(255,255,255,0.58)',
    fontSize: 12,
    textAlign: 'center',
  },
});

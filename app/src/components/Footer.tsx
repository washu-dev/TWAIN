import React from 'react';
import { View, Text, StyleSheet } from 'react-native';
import { Colors, Spacing } from '@/constants/theme';

const C = Colors.light;

export const Footer: React.FC = () => {
  return (
    <View style={styles.container}>
      <View style={styles.topBar} />
      {/* The address stays -- it is institutional attribution, not filler. What
          was actually costing ~90pt of every screen was the padding around it, so
          that is what shrank: same two lines, a little over half the height. */}
      <View style={styles.content}>
        <Text style={styles.brandName}>Washington University in St. Louis</Text>
        <Text style={styles.address}>One Brookings Drive, St. Louis, MO 63130</Text>
      </View>
    </View>
  );
};

const styles = StyleSheet.create({
  container: {
    backgroundColor: C.surfaceDark,
    marginTop: Spacing.four,
  },
  topBar: {
    height: 4,
    backgroundColor: C.washuRed,
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
  },
  address: {
    color: C.textOnDarkMuted,
    fontSize: 12,
    textAlign: 'center',
  },
});

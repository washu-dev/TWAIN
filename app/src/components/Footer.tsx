import React from 'react';
import { View, Text, StyleSheet } from 'react-native';
import { Colors, Spacing } from '@/constants/theme';

const C = Colors.light;

export const Footer: React.FC = () => {
  return (
    <View style={styles.container}>
      <View style={styles.topBar} />
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
    paddingVertical: Spacing.four,
    alignItems: 'center',
    gap: Spacing.one,
  },
  brandName: {
    color: C.textOnDark,
    fontSize: 14,
    fontWeight: '700',
    textAlign: 'center',
  },
  address: {
    color: C.textOnDarkMuted,
    fontSize: 12,
    textAlign: 'center',
  },
});

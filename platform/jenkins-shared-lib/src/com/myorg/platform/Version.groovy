// jenkins-shared-lib/src/com/myorg/platform/Version.groovy
// Helper for semver comparison used by validateDependencies.groovy
package com.myorg.platform

class Version implements Comparable<Version>, Serializable {
    final int major, minor, patch

    static Version parse(String v) {
        def clean = v.replaceAll(/[^0-9.]/, '').tokenize('.')
        return new Version(
            major: clean.size() > 0 ? clean[0].toInteger() : 0,
            minor: clean.size() > 1 ? clean[1].toInteger() : 0,
            patch: clean.size() > 2 ? clean[2].toInteger() : 0,
        )
    }

    /** Check if this version satisfies a constraint like ">=2.2.0" or "^1.3.0" */
    boolean satisfies(String constraint) {
        def matcher = constraint =~ /^([><=^~!]{1,2})\s*([\d.]+)$/
        if (!matcher) return true  // no recognisable constraint → pass
        def op      = matcher[0][1]
        def required = Version.parse(matcher[0][2])
        switch (op) {
            case '>=': return this >= required
            case '>':  return this >  required
            case '<=': return this <= required
            case '<':  return this <  required
            case '==': return this == required
            case '^':  return this.major == required.major && this >= required
            case '~':  return this.major == required.major &&
                              this.minor == required.minor &&
                              this >= required
            default:   return true
        }
    }

    @Override
    int compareTo(Version o) {
        if (major != o.major) return major <=> o.major
        if (minor != o.minor) return minor <=> o.minor
        return patch <=> o.patch
    }

    @Override
    String toString() { "${major}.${minor}.${patch}" }
}

import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

// RTL only auto-registers cleanup when vitest globals are on; they are not, so
// unmount explicitly. Without this, renders accumulate across tests and queries
// start matching leftovers from earlier cases.
afterEach(cleanup)

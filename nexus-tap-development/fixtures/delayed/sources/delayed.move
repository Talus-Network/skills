module delayed_fixture::delayed;

const E_SCHEDULE_INPUT_TOO_LARGE: u64 = 0;
const E_INVALID_SCHEDULED_STATE: u64 = 1;
const MAX_SCHEDULED_INPUT: u64 = 99;

public struct DelayedState has copy, drop {
    scheduled: u64,
    executed: u64,
}

public fun schedule(input: u64): DelayedState {
    assert!(input <= MAX_SCHEDULED_INPUT, E_SCHEDULE_INPUT_TOO_LARGE);
    DelayedState { scheduled: input, executed: 0 }
}

public fun execute(state: &mut DelayedState): u64 {
    assert!(state.scheduled <= MAX_SCHEDULED_INPUT, E_INVALID_SCHEDULED_STATE);
    state.executed = state.scheduled + 1;
    state.executed
}

#[test_only]
public fun invalid_scheduled_state_for_test(): DelayedState {
    DelayedState { scheduled: 100, executed: 0 }
}

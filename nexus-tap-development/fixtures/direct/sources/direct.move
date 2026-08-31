module direct_fixture::direct;

const E_BEGIN_INPUT_TOO_LARGE: u64 = 0;
const E_EXECUTE_INPUT_TOO_LARGE: u64 = 1;
const MAX_INPUT: u64 = 99;

public struct DirectState has copy, drop {
    value: u64,
}

public fun begin(input: u64): DirectState {
    assert!(input <= MAX_INPUT, E_BEGIN_INPUT_TOO_LARGE);
    DirectState { value: input + 1 }
}

public fun execute(state: &mut DirectState, input: u64): u64 {
    assert!(input <= MAX_INPUT, E_EXECUTE_INPUT_TOO_LARGE);
    state.value = state.value + input;
    state.value
}

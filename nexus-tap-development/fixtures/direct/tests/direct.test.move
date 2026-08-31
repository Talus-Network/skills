#[test_only]
module direct_fixture::direct_tests;

use direct_fixture::direct;

#[test]
fun direct_path_produces_and_completes_output() {
    let mut state = direct::begin(40);
    assert!(direct::execute(&mut state, 1) == 42, 0);
}

#[test]
fun direct_path_handles_zero_input() {
    let mut state = direct::begin(0);
    assert!(direct::execute(&mut state, 2) == 3, 1);
}

#[test, expected_failure(abort_code = 0)]
fun direct_begin_rejects_input_above_limit() {
    let _state = direct::begin(100);
}

#[test, expected_failure(abort_code = 1)]
fun direct_execute_rejects_input_above_limit() {
    let mut state = direct::begin(0);
    let _ = direct::execute(&mut state, 100);
}

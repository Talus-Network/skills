#[test_only]
module delayed_fixture::delayed_tests;

use delayed_fixture::delayed;

#[test]
fun delayed_path_keeps_scheduled_input_until_follow_up() {
    let mut state = delayed::schedule(41);
    assert!(delayed::execute(&mut state) == 42, 0);
}

#[test]
fun delayed_path_has_distinct_follow_up_result() {
    let mut state = delayed::schedule(0);
    assert!(delayed::execute(&mut state) == 1, 1);
}

#[test, expected_failure(abort_code = 0)]
fun delayed_schedule_rejects_input_above_limit() {
    let _state = delayed::schedule(100);
}

#[test, expected_failure(abort_code = 1)]
fun delayed_execute_rejects_invalid_scheduled_state() {
    let mut state = delayed::invalid_scheduled_state_for_test();
    let _ = delayed::execute(&mut state);
}

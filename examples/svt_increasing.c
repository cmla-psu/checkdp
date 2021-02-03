int sparsevector(float q[], int size, float epsilon, float T, int NN)
{
  "TYPES: epsilon: <0, 0>; size: <0, 0>; q: <*, *>; T: <0, 0>; NN: <0, 0>";
  "PRECONDITION: INCREASING; ASSUME(NN > 0); ASSUME(NN <= size); ASSUME(T >= -10); ASSUME(T <= 10);";
  "CHECK: epsilon";

  int out = 0;
  float eta_1 = Lap(2 / epsilon);
  int T_bar = T + eta_1;
  int count = 0;
  int i = 0;

  while (count < NN && i < size)
  {
    float eta_2 = Lap(2 * NN / epsilon);

    if (q[i] + eta_2 >= T_bar)
    {
      CHECKDP_OUTPUT(1);
      count = count + 1;
    }
    else
    {
      CHECKDP_OUTPUT(0);
    }
    i = i + 1;
  }
}
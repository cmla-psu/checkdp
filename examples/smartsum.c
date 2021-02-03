int smartsum(float q[], int size, float epsilon, float T, int M)
{
  "TYPES: epsilon: <0, 0>; size: <0, 0>; q: <*, *>; T: <0, 0>; M: <0, 0>";
  "PRECONDITION: ONE_DIFFER; ASSUME(T >= 0); ASSUME(T < size); ASSUME(M > 1); ASSUME(M < size);";
  "CHECK: 2 * epsilon";

  float next = 0; int i = 0; float sum = 0;
  while(i <= T && i < size)
  {
    if ((i + 1) % M == 0)
    {
      float eta_1 = Lap(1 / epsilon);
      next = sum + q[i] + eta_1;
      sum = 0;
      CHECKDP_OUTPUT(next);
    }
    else
    {
      float eta_2 = Lap(1 / epsilon);
      next = next + q[i] + eta_2;
      sum = sum + q[i];
      CHECKDP_OUTPUT(next);
    }
    i = i + 1;
  }
}